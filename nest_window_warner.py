#!/usr/bin/env python3
"""
Nest Window Warner
==================
Monitors Google Nest indoor/outdoor temperatures from a Chrome window via
screenshot + OCR. Alerts when outdoor temperature nears indoor temperature.

Features:
  - Window capture by partial title match
  - Calibration mode to select temperature ROIs via GUI
  - EasyOCR with OpenCV preprocessing for reliable digit reading
  - Configurable polling interval
  - Audible alert (Windows beep) when outdoor >= indoor - margin
  - CSV logging of all readings
  - Test mode for static screenshot files

Setup:
  pip install pygetwindow pillow opencv-python easyocr

Usage:
  python nest_monitor.py                  # Run monitor loop
  python nest_monitor.py --calibrate      # Set up ROIs
  python nest_monitor.py --test-image screenshot.png  # Test OCR on a static image

Configuration is stored in config.json (created after first calibration).
"""

import argparse
import ctypes
import csv
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nestmon")

# ---------------------------------------------------------------------------
# Platform helpers
# ---------------------------------------------------------------------------
IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Windows DWM / high-DPI capture utilities
# ---------------------------------------------------------------------------

# DWM constants
DWMWA_EXTENDED_FRAME_BOUNDS = 9
PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0


class _WINRECT(ctypes.Structure):
    """Windows RECT structure for DwmGetWindowAttribute."""
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    """Windows BITMAPINFOHEADER used to copy a Win32 bitmap into Python bytes."""
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    """Windows BITMAPINFO wrapper with no colour table for 32-bit RGB data."""
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER)]


def _get_window_physical_bounds(window):
    """Return the window's on-screen rectangle in **physical** pixels.

    Uses ``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`` which
    bypasses DPI virtualisation entirely - the returned rect is always in
    raw device pixels regardless of the system DPI scaling setting.

    Returns ``(left, top, right, bottom)`` or *None* on failure.
    """
    if not IS_WINDOWS:
        left = max(window.left, 0)
        top = max(window.top, 0)
        return (left, top, left + window.width, top + window.height)

    try:
        hwnd = window._hWnd
        if not hwnd:
            raise ValueError("no HWND on pygetwindow object")

        rect = _WINRECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            ctypes.c_uint(DWMWA_EXTENDED_FRAME_BOUNDS),
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr != 0:  # S_OK
            log.debug("DwmGetWindowAttribute returned HRESULT 0x%08X", hr)
            return None

        left = max(rect.left, 0)
        top = max(rect.top, 0)
        log.debug(
            "DWM extended bounds for '%s': (%d, %d, %d, %d)  %dx%d",
            window.title, left, top, rect.right, rect.bottom,
            rect.right - left, rect.bottom - top,
        )
        return (left, top, rect.right, rect.bottom)
    except Exception as e:
        log.debug("_get_window_physical_bounds failed: %s", e)
        return None


def beep(freq=1000, duration_ms=800):
    """Emit an audible beep.

    Uses ``winsound.Beep`` on Windows.  Fires a terminal bell (``\\a``) as
    fallback if winsound is unavailable or fails.  The longer default
    duration (800 ms vs the old 400 ms) helps survive Dolby Atmos / spatial
    audio driver wake-up gaps (~200-300 ms of silence while the audio
    pipeline powers on).
    """
    if IS_WINDOWS:
        try:
            import winsound
            winsound.Beep(freq, duration_ms)
            return
        except Exception:
            pass  # fall through to terminal bell
    sys.stdout.write("\a")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "window_title": "Nest",
    "polling_interval_seconds": 60,
    "alert_margin": 0.5,
    "beep_enabled": True,
    "beep_duration_ms": 800,       # longer beep survives Dolby Atmos audio wake-up gap
    "beep_interval_sec": 1.5,      # seconds between consecutive beeps during alert
    "indoor_roi": None,  # [x, y, w, h] in window-relative pixels
    "outdoor_roi": None,  # [x, y, w, h]
    "csv_log_path": "nest_readings.csv",
    "capture_method": "auto",  # auto | print_window | screen_crop | foreground_crop
    "debug_capture": False,   # save debug screenshots to disk
    "restore_before_capture": True,   # attempt window.restore() if minimised/invalid
    "maximize_on_restore": False,     # also maximise after restore (default off)
}

CONFIG_PATH = Path(__file__).with_name("config.json")


def load_config(path=None):
    """Load configuration from JSON file, falling back to defaults."""
    path = Path(path) if path else CONFIG_PATH
    if path.exists():
        try:
            with open(path, "r") as f:
                cfg = json.load(f)
            # Merge with defaults for any missing keys
            merged = {**DEFAULT_CONFIG, **cfg}
            log.info("Loaded config from %s", path)
            return merged
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Could not parse config (%s), using defaults", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg, path=None):
    """Save configuration dictionary as JSON."""
    path = Path(path) if path else CONFIG_PATH
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    log.info("Config saved to %s", path)


# ---------------------------------------------------------------------------
# Screenshot / window capture
# ---------------------------------------------------------------------------
def _get_gw():
    """Lazy-import pygetwindow with error handling."""
    try:
        import pygetwindow as gw
        return gw
    except ImportError:
        log.error("pygetwindow is required.  Install with:  pip install pygetwindow")
        sys.exit(1)


def list_visible_windows():
    """Return a list of (title, window_obj) for every non-empty titled window."""
    gw = _get_gw()
    result = []
    for w in gw.getAllWindows():
        t = w.title.strip()
        if t:
            result.append((t, w))
    return result


def _format_window_row(i, title, w, marker=""):
    """Shared helper: format a single window row for display."""
    minimised = w.width <= 0
    size = f"{w.width}x{w.height}" if not minimised else "! minimised"
    display_title = title if len(title) <= 54 else title[:51] + "..."
    return f"{i:<4} {display_title:<57} {size}{marker}"


def list_windows_command():
    """Print all visible window titles to stdout (used by --list-windows)."""
    windows = list_visible_windows()
    if not windows:
        print("No visible windows found.")
        return
    print(f"\n{'#':<4} {'Title':<57} {'Size'}")
    print("-" * 80)
    for i, (title, w) in enumerate(windows, 1):
        print(_format_window_row(i, title, w))
    print()


def pick_window_interactive(title_substring):
    """
    Present an interactive numbered list of ALL visible windows and let the
    user choose one.  Matching windows (by *title_substring*) are highlighted.

    Even when there is exactly one match we still show the full list so the
    user can confirm before proceeding - this avoids silently grabbing the
    wrong window on multi-monitor / ambiguous-title setups.

    Returns (window, chosen_title_string) or (None, None) if the user cancels.
    """
    gw = _get_gw()

    # Build a set of matched titles for highlighting
    match_titles = {w.title.strip() for w in gw.getWindowsWithTitle(title_substring)}
    all_windows = list_visible_windows()

    if not all_windows:
        log.error("No visible windows found at all.")
        return None, None

    # Show a summary line
    n_matched = sum(1 for t, _ in all_windows if t in match_titles)
    if n_matched == 0:
        print(f"\n  !  No window matched '{title_substring}' - showing all windows:")
    elif n_matched == 1:
        matched_title = next(t for t, _ in all_windows if t in match_titles)
        print(f"\n  OK  1 window matched '{title_substring}':  \"{matched_title}\"")
    else:
        print(f"\n  OK  {n_matched} windows matched '{title_substring}'")

    print("  Pick a window by number (or 0 to cancel):\n")
    print(f"{'#':<4} {'Title':<57} {'Size'}")
    print("-" * 80)
    for i, (title, w) in enumerate(all_windows, 1):
        marker = " <- match" if title in match_titles else ""
        print(_format_window_row(i, title, w, marker))
    print()

    while True:
        try:
            choice = input("Enter number (0 to cancel): ").strip()
            if choice == "0" or choice == "":
                return None, None
            idx = int(choice) - 1
            if 0 <= idx < len(all_windows):
                chosen_title, chosen_window = all_windows[idx]
                if chosen_window.width <= 0:
                    log.warning(
                        "Window '%s' is minimised - restore it and try again.",
                        chosen_title,
                    )
                log.info("User selected window: '%s'", chosen_title)
                return chosen_window, chosen_title
            print(f"  Invalid number. Pick 1-{len(all_windows)} or 0 to cancel.")
        except (ValueError, EOFError):
            print("  Please enter a number.")


def find_chrome_window(title_substring):
    """Return the pygetwindow Win32Window whose title contains *title_substring*."""
    gw = _get_gw()
    windows = gw.getWindowsWithTitle(title_substring)
    if not windows:
        log.warning(
            "No window found containing '%s'. Visible windows:", title_substring
        )
        for w in gw.getAllWindows():
            if w.title.strip():
                log.warning("  title='%s'", w.title)
        return None

    # Prefer a non-empty title match
    for w in windows:
        if w.title.strip():
            log.info("Matched window: '%s'", w.title)
            return w
    return windows[0]


def _capture_screen_crop(window, debug_capture=False):
    """Strategy 1 - DWM extended-frame bounds + full-screen crop.

    Uses ``DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)`` to get the
    window's true physical pixel rectangle, then grabs the entire virtual
    desktop and crops to that rectangle.  Robust across all DPI settings.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        log.error("Pillow required: pip install pillow")
        return None

    bounds = _get_window_physical_bounds(window)
    if bounds is None:
        # Fall back to pygetwindow logical coords as best-effort
        log.debug("DWM bounds unavailable, using pygetwindow logical coords")
        left = max(window.left, 0)
        top = max(window.top, 0)
        bounds = (left, top, left + window.width, top + window.height)

    left, top, right, bottom = [int(v) for v in bounds]
    width = right - left
    height = bottom - top

    log.debug(
        "screen_crop: '%s' -> rect=(%d,%d,%d,%d)  %dx%d",
        window.title, left, top, right, bottom, width, height,
    )

    if width <= 0 or height <= 0:
        log.error("Window '%s' has invalid geometry: %dx%d - minimised?",
                  window.title, width, height)
        return None

    # Capture the entire virtual desktop
    try:
        full = ImageGrab.grab(all_screens=True)
        log.debug("  full-screen grab = %dx%d", full.width, full.height)
    except Exception as e:
        log.error("Full-screen grab failed: %s", e)
        return None

    # Clamp crop bounds to the captured image
    if right > full.width or bottom > full.height:
        log.warning(
            "  bounds (%d,%d,%d,%d) exceed screen (%d,%d) - clamping",
            left, top, right, bottom, full.width, full.height,
        )
        right = min(right, full.width)
        bottom = min(bottom, full.height)

    try:
        img = full.crop((left, top, right, bottom))
        log.debug("  cropped to %dx%d", img.width, img.height)
    except Exception as e:
        log.error("Crop failed: %s", e)
        return None

    if debug_capture:
        _save_debug_image(img, "screen_crop")

    return img


def _capture_foreground_crop(window, debug_capture=False):
    """Strategy 2 - activate window, brief sleep, full-screen grab + crop.

    More intrusive (steals focus briefly) but works when DWM queries fail.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None

    log.debug("foreground_crop: activating window '%s' ...", window.title)

    try:
        window.activate()
        time.sleep(0.4)  # allow the window to paint
    except Exception as e:
        log.warning("Could not activate window: %s", e)

    # Re-read geometry after activation (may have changed)
    bounds = _get_window_physical_bounds(window)
    if bounds is None:
        left = max(window.left, 0)
        top = max(window.top, 0)
        bounds = (left, top, left + window.width, top + window.height)

    left, top, right, bottom = [int(v) for v in bounds]
    width = right - left
    height = bottom - top

    if width <= 0 or height <= 0:
        log.error("Window '%s' has invalid geometry after activation",
                  window.title)
        return None

    try:
        full = ImageGrab.grab(all_screens=True)
        img = full.crop((left, top, right, bottom))
        log.debug("foreground_crop: captured %dx%d", img.width, img.height)
    except Exception as e:
        log.error("foreground_crop failed: %s", e)
        return None

    if debug_capture:
        _save_debug_image(img, "foreground_crop")

    return img


def _capture_print_window(window, debug_capture=False):
    """Strategy 3 - ask Windows to render the chosen window off-screen.

    This is the closest thing to a true "background screenshot" on Windows:
    the selected window can sit behind other windows and still be captured.
    Chrome may need hardware acceleration disabled if it paints a blank image.
    Minimized windows are still unreliable because many apps stop rendering.
    """
    if not IS_WINDOWS:
        log.error("print_window capture is only available on Windows.")
        return None

    try:
        from PIL import Image
    except ImportError:
        log.error("Pillow required: pip install pillow")
        return None

    hwnd = getattr(window, "_hWnd", None)
    if not hwnd:
        log.error("Selected window has no Win32 HWND.")
        return None

    bounds = _get_window_physical_bounds(window)
    if bounds is None:
        left = max(window.left, 0)
        top = max(window.top, 0)
        bounds = (left, top, left + window.width, top + window.height)

    left, top, right, bottom = [int(v) for v in bounds]
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        log.error("Window '%s' has invalid geometry: %dx%d - minimised?",
                  window.title, width, height)
        return None

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_uint]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int
    ]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
        wintypes.LPVOID, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int

    hwnd = wintypes.HWND(hwnd)
    window_dc = user32.GetWindowDC(hwnd)
    if not window_dc:
        log.error("GetWindowDC failed for '%s'", window.title)
        return None

    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)

    try:
        # PW_RENDERFULLCONTENT helps with Chromium/modern Windows, but not every
        # app honours it. A zero flag fallback catches older window renderers.
        rendered = user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
        if not rendered:
            log.debug("PrintWindow full-content failed; retrying without flags")
            rendered = user32.PrintWindow(hwnd, memory_dc, 0)
        if not rendered:
            log.warning("PrintWindow returned no image for '%s'", window.title)
            return None

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down DIB avoids vertical flip
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bmi.bmiHeader.biSizeImage = width * height * 4

        buffer = ctypes.create_string_buffer(bmi.bmiHeader.biSizeImage)
        copied = gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, buffer, ctypes.byref(bmi),
            DIB_RGB_COLORS,
        )
        if copied != height:
            log.warning("GetDIBits copied %s/%s rows", copied, height)
            return None

        img = Image.frombuffer(
            "RGB", (width, height), buffer, "raw", "BGRX", 0, 1
        ).copy()
        log.debug("print_window: captured %dx%d from '%s'",
                  img.width, img.height, window.title)
    finally:
        if old_bitmap:
            gdi32.SelectObject(memory_dc, old_bitmap)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)

    if debug_capture:
        _save_debug_image(img, "print_window")

    return img


def _save_debug_image(img, tag="debug"):
    """Save a debug capture image to disk."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(__file__).with_name(f"debug_{tag}_{ts}.png")
    try:
        img.save(path)
        log.info("Debug capture saved: %s", path)
    except Exception as e:
        log.warning("Could not save debug capture: %s", e)


def ensure_window_restored(window, maximize=False, activate=False):
    """Attempt to restore a minimised / invalid-geometry window.

    This is a Windows-only helper.  On other platforms it is a no-op.

    It never steals focus by default (*activate* = *False*).

    Parameters
    ----------
    maximize : bool
        If *True*, also maximise the window after restoring it.
        Default *False* — restoring to previous size is less disruptive.
    activate : bool
        If *True*, bring the window to the foreground.  Default *False*.

    Returns
    -------
    bool
        *True* if the window bounds are now valid, *False* otherwise.
    """
    if not IS_WINDOWS:
        return True  # can't restore on non-Windows; assume it's fine

    # Check current geometry
    bounds = _get_window_physical_bounds(window)
    if bounds is None:
        left, top = max(window.left, 0), max(window.top, 0)
        width, height = window.width, window.height
    else:
        left, top, right, bottom = bounds
        width = right - left
        height = bottom - top

    if width > 0 and height > 0:
        return True  # already valid

    log.info(
        "Window '%s' appears minimised/invalid (geom %dx%d); attempting restore",
        window.title, width, height,
    )

    try:
        window.restore()
        time.sleep(0.5)  # let the window manager settle
    except Exception as e:
        log.warning("window.restore() failed: %s", e)
        return False

    # Re-check geometry after restore
    bounds = _get_window_physical_bounds(window)
    if bounds is None:
        width2, height2 = window.width, window.height
    else:
        left2, top2, right2, bottom2 = bounds
        width2 = right2 - left2
        height2 = bottom2 - top2

    if width2 <= 0 or height2 <= 0:
        log.warning(
            "Restore did not fix geometry for '%s' (still %dx%d)",
            window.title, width2, height2,
        )
        return False

    log.info(
        "Restore succeeded for '%s' -> %dx%d; retrying capture",
        window.title, width2, height2,
    )

    if maximize:
        try:
            window.maximize()
            time.sleep(0.3)
            log.debug("Maximised '%s' after restore", window.title)
        except Exception as e:
            log.warning("window.maximize() failed: %s", e)
            # non-fatal — we still have valid geometry
    else:
        log.debug("Skipping maximize because maximize_on_restore=false")

    if activate:
        try:
            window.activate()
            time.sleep(0.2)
        except Exception as e:
            log.warning("window.activate() failed: %s", e)

    return True


def _dispatch_capture(window, capture_method, debug_capture):
    """Run the actual capture backend, returning PIL.Image or None."""
    if capture_method == "print_window":
        return _capture_print_window(window, debug_capture)

    if capture_method == "screen_crop":
        return _capture_screen_crop(window, debug_capture)

    if capture_method == "foreground_crop":
        return _capture_foreground_crop(window, debug_capture)

    # "auto" - try background rendering first, then fall back if needed.
    img = _capture_print_window(window, debug_capture)
    if img is not None:
        return img

    img = _capture_screen_crop(window, debug_capture)
    if img is not None:
        return img

    log.warning("screen_crop failed - falling back to foreground_crop "
                "(will briefly steal focus). "
                "Consider using --capture-method print_window or screen_crop if foreground "
                "captures are disruptive.")
    return _capture_foreground_crop(window, debug_capture)


def capture_window_region(window, capture_method="auto", debug_capture=False,
                          restore_before_capture=True, maximize_on_restore=False):
    """Capture the content of *window* using the configured strategy.

    Parameters
    ----------
    capture_method : str
        - ``"auto"`` - try *print_window*, then less direct fallbacks.
        - ``"print_window"`` - Win32 background render. Can work behind other
          windows without stealing focus.
        - ``"screen_crop"`` - DWM extended-frame bounds + full-screen crop.
        - ``"foreground_crop"`` - activate window -> brief sleep -> full-screen
          grab + crop.  Steals focus briefly but is most compatible.
    debug_capture : bool
        If *True*, save the captured image to disk for inspection.
    restore_before_capture : bool
        If *True* (default), attempt ``window.restore()`` when the window
        appears minimised or has invalid geometry.
    maximize_on_restore : bool
        If *True*, also maximise after a successful restore.  Default *False*.

    Returns
    -------
    PIL.Image or None
    """
    # First attempt — check geometry and restore if needed
    if restore_before_capture:
        valid = ensure_window_restored(
            window, maximize=maximize_on_restore, activate=False,
        )
        if not valid:
            log.warning(
                "Window '%s' still has invalid geometry after restore "
                "attempt; capture will likely fail.",
                window.title,
            )
            # Fall through anyway — some backends might still work

    img = _dispatch_capture(window, capture_method, debug_capture)
    if img is not None:
        return img

    # If the first attempt failed AND we didn't try restoring yet,
    # try restoring now and retry once
    if not restore_before_capture:
        valid = ensure_window_restored(
            window, maximize=maximize_on_restore, activate=False,
        )
        if valid:
            log.info("Retrying capture after restore for '%s'", window.title)
            img = _dispatch_capture(window, capture_method, debug_capture)
            if img is not None:
                return img

    return None


def capture_window(title_substring, cfg):
    """High-level capture: find window, grab screenshot."""
    window = find_chrome_window(title_substring)
    if window is None:
        return None, None
    method = cfg.get("capture_method", "auto")
    debug = cfg.get("debug_capture", False)
    restore = cfg.get("restore_before_capture", True)
    maximize = cfg.get("maximize_on_restore", False)
    img = capture_window_region(window, capture_method=method,
                                debug_capture=debug,
                                restore_before_capture=restore,
                                maximize_on_restore=maximize)
    return img, window


# ---------------------------------------------------------------------------
# Calibration GUI  (tkinter)
# ---------------------------------------------------------------------------
class CalibrationApp:
    """
    Tkinter window that displays the screenshot and lets the user draw
    two ROI rectangles (indoor = red, outdoor = blue).

    Coordinates are stored at original (pre-scaling) image resolution.
    """

    def __init__(self, pil_image, config_path=None):
        self.config_path = Path(config_path) if config_path else CONFIG_PATH
        self.original = pil_image
        self.orig_w, self.orig_h = pil_image.size

        # Scale image to fit screen
        import tkinter as tk

        root_temp = tk.Tk()
        root_temp.withdraw()
        screen_w = root_temp.winfo_screenwidth()
        screen_h = root_temp.winfo_screenheight()
        root_temp.destroy()

        max_w = int(screen_w * 0.85)
        max_h = int(screen_h * 0.85)
        self.scale = min(max_w / self.orig_w, max_h / self.orig_h, 1.0)
        self.disp_w = int(self.orig_w * self.scale)
        self.disp_h = int(self.orig_h * self.scale)

        # Resize for display
        from PIL import ImageTk

        self.display_img = self.original.resize(
            (self.disp_w, self.disp_h)
        )

        # ROI storage (original coordinates)
        self.indoor_roi = None  # (x, y, w, h)
        self.outdoor_roi = None

        # Drawing state
        self.drawing = False
        self.rect_id = None
        self.start_x = self.start_y = 0
        self.next_roi = "indoor"  # "indoor" then "outdoor" then "done"
        self.indoor_rect_id = None
        self.outdoor_rect_id = None

        # Build UI
        self.root = tk.Tk()
        self.root.title("Nest Monitor - Calibration")
        self.canvas = tk.Canvas(
            self.root, width=self.disp_w, height=self.disp_h, cursor="cross"
        )
        self.canvas.pack()

        self.photo = ImageTk.PhotoImage(self.display_img)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)

        # Status bar
        self.status = tk.Label(
            self.root,
            text="Draw INDOOR temperature ROI (red). Click & drag.  [r]=reset  [Enter]=save",
            bg="#333",
            fg="#fff",
            font=("Consolas", 11),
        )
        self.status.pack(fill=tk.X)

        # Bindings
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<r>", lambda e: self.reset())
        self.root.bind("<R>", lambda e: self.reset())
        self.root.bind("<Return>", lambda e: self.save_and_exit())
        self.root.bind("<Escape>", lambda e: self.root.destroy())
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _to_original(self, x, y):
        """Convert display coordinates to original image coordinates."""
        return int(x / self.scale), int(y / self.scale)

    def on_press(self, event):
        if self.next_roi == "done":
            self.status.config(text="Both ROIs set. Press Enter to save, 'r' to redo.")
            return
        self.drawing = True
        self.start_x, self.start_y = event.x, event.y
        color = "red" if self.next_roi == "indoor" else "blue"
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline=color, width=2
        )

    def on_drag(self, event):
        if not self.drawing or self.rect_id is None:
            return
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

    def on_release(self, event):
        if not self.drawing:
            return
        self.drawing = False

        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y
        if abs(x2 - x1) < 5 or abs(y2 - y1) < 5:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
            return

        # Normalize
        dx, dy = min(x1, x2), min(y1, y2)
        dw, dh = abs(x2 - x1), abs(y2 - y1)

        # Convert to original coords
        ox, oy = self._to_original(dx, dy)
        ow, oh = self._to_original(dw, dh)

        if self.next_roi == "indoor":
            self.indoor_roi = (ox, oy, ow, oh)
            self.indoor_rect_id = self.rect_id
            self.next_roi = "outdoor"
            self.status.config(
                text="Draw OUTDOOR temperature ROI (blue). Click & drag.  [r]=reset  [Enter]=save"
            )
        else:
            self.outdoor_roi = (ox, oy, ow, oh)
            self.outdoor_rect_id = self.rect_id
            self.next_roi = "done"
            self.status.config(
                text="Both ROIs set!  Press Enter to save, 'r' to redo."
            )

        self.rect_id = None

    def reset(self):
        """Clear both ROIs and start over."""
        for rid in (self.indoor_rect_id, self.outdoor_rect_id):
            if rid is not None:
                self.canvas.delete(rid)
        self.indoor_rect_id = self.outdoor_rect_id = None
        self.indoor_roi = self.outdoor_roi = None
        self.next_roi = "indoor"
        self.status.config(
            text="Draw INDOOR temperature ROI (red). Click & drag.  [r]=reset  [Enter]=save"
        )

    def save_and_exit(self):
        if self.indoor_roi is None or self.outdoor_roi is None:
            self.status.config(text="ERROR: Both ROIs must be set before saving!")
            return

        cfg = load_config(self.config_path)
        cfg["indoor_roi"] = list(self.indoor_roi)
        cfg["outdoor_roi"] = list(self.outdoor_roi)
        save_config(cfg, self.config_path)
        log.info(
            "Calibration saved. indoor_roi=%s outdoor_roi=%s",
            self.indoor_roi,
            self.outdoor_roi,
        )
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---------------------------------------------------------------------------
# OCR  (EasyOCR + OpenCV preprocessing)
# ---------------------------------------------------------------------------
_easyocr_reader = None
_easyocr_lock = threading.Lock()


def _get_reader():
    """Lazy-init EasyOCR reader (thread-safe)."""
    global _easyocr_reader
    if _easyocr_reader is None:
        with _easyocr_lock:
            if _easyocr_reader is None:
                try:
                    import easyocr

                    log.info("Initialising EasyOCR (may download models on first run) ...")
                    _easyocr_reader = easyocr.Reader(
                        ["en"], gpu=False, verbose=False
                    )
                    log.info("EasyOCR ready.")
                except ImportError:
                    log.error(
                        "EasyOCR is required.  Install with:  pip install easyocr"
                    )
                    sys.exit(1)
    return _easyocr_reader


def preprocess_roi(pil_image):
    """
    Preprocess a cropped PIL image for OCR:
      - Convert to grayscale
      - Enlarge 3x (helps EasyOCR with small digits)
      - Apply CLAHE contrast enhancement
      - Apply OTSU binary threshold
    Returns a numpy array (OpenCV image).
    """
    import cv2
    import numpy as np

    # PIL -> numpy (RGB)
    img = np.array(pil_image)
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        img = img.copy()

    h, w = img.shape[:2]
    if h < 20 or w < 20:
        # Too small to be meaningful
        return img

    # Enlarge 3x
    img = cv2.resize(img, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)

    # CLAHE for local contrast
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img = clahe.apply(img)

    # OTSU binary threshold
    _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Invert if most pixels are white (assume dark text on light bg)
    white_ratio = np.mean(img > 127)
    if white_ratio > 0.6:
        img = cv2.bitwise_not(img)

    return img


def parse_temperature_token(text):
    """Convert an OCR token into Celsius, including Nest's small decimal digit.

    Google/Nest can draw 30.5 as a large "30" plus a smaller "5". OCR often
    sees that as "305". Treat a plausible 3-digit token as tenths, so 305
    becomes 30.5. This is like cleaning a data feed before estimating from it:
    fix the known measurement convention, then keep the plausibility bounds.
    """
    cleaned = text.strip().replace(",", ".")
    if not re.fullmatch(r"-?\d{1,3}(?:\.\d)?", cleaned):
        return None

    try:
        val = float(cleaned)
    except ValueError:
        return None

    if -30 <= val <= 55:
        return val

    signless = cleaned.lstrip("-")
    if "." not in cleaned and signless.isdigit() and len(signless) == 3:
        tenths = val / 10.0
        if -30 <= tenths <= 55:
            return tenths

    return None


def read_temperature(pil_roi, label="temp"):
    """
    Run EasyOCR on a preprocessed ROI crop and extract a temperature value.

    Returns (temperature_float, confidence) or (None, 0) on failure.
    """
    reader = _get_reader()
    preprocessed = preprocess_roi(pil_roi)

    try:
        results = reader.readtext(preprocessed)
    except Exception as e:
        log.warning("OCR error for %s: %s", label, e)
        return None, 0

    if not results:
        log.debug("OCR found no text in %s ROI", label)
        return None, 0

    candidates = []
    for bbox, text, conf in results:
        text = text.strip()
        # Find integer substrings (2-3 digits, no decimal).
        # Nest's tiny degree-symbol (°) often OCRs as "8", turning
        # "30°" into "30.8".  By matching only raw digits we skip the
        # misread symbol while still catching "305" → 30.5 via
        # parse_temperature_token's 3-digit heuristic.
        for match in re.finditer(r"-?\d{2,3}", text):
            val = parse_temperature_token(match.group())
            if val is not None:
                candidates.append((val, conf))

    if not candidates:
        log.debug("No plausible temperature in %s ROI. OCR texts: %s",
                  label, [r[1] for r in results])
        return None, 0

    # Pick highest confidence
    candidates.sort(key=lambda x: x[1], reverse=True)
    best_val, best_conf = candidates[0]
    log.debug("%s OCR -> %.1f (conf=%.2f) from %d candidates",
              label, best_val, best_conf, len(candidates))
    return best_val, best_conf


# ---------------------------------------------------------------------------
# Alert management
# ---------------------------------------------------------------------------
alert_stop_event = threading.Event()
alert_thread_ref = None
alert_lock = threading.Lock()


def _alert_beep_loop(cfg):
    """Beep continuously until alert_stop_event is set."""
    duration_ms = cfg.get("beep_duration_ms", 800)
    interval_sec = cfg.get("beep_interval_sec", 1.5)
    while not alert_stop_event.is_set():
        beep(1200, duration_ms)
        # Sleep in small chunks to stay responsive to stop event
        deadline = time.time() + interval_sec
        while time.time() < deadline and not alert_stop_event.is_set():
            time.sleep(0.1)


def start_alert(cfg=None):
    """Start the beep thread if not already running."""
    global alert_thread_ref
    if cfg is None:
        cfg = {}
    with alert_lock:
        if alert_thread_ref is not None and alert_thread_ref.is_alive():
            return  # already beeping
        alert_stop_event.clear()
        alert_thread_ref = threading.Thread(
            target=_alert_beep_loop, args=(cfg,), daemon=True,
        )
        alert_thread_ref.start()
        log.info("ALERT started - outdoor temperature is near/above indoor")


def stop_alert():
    """Stop the beep thread."""
    global alert_thread_ref
    alert_stop_event.set()
    with alert_lock:
        alert_thread_ref = None
    log.info("Alert acknowledged / cleared")


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------
def append_csv(csv_path, timestamp, indoor, outdoor, status):
    """Append one row to the CSV log file (creates header if new)."""
    file_exists = Path(csv_path).exists()
    try:
        with open(csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "indoor", "outdoor", "status"])
            writer.writerow([timestamp, indoor, outdoor, status])
    except OSError as e:
        log.warning("CSV write failed: %s", e)


# ---------------------------------------------------------------------------
# Monitoring loop
# ---------------------------------------------------------------------------
def read_temperatures(img, cfg):
    """Extract indoor and outdoor temperatures from a screenshot."""
    indoor_roi = cfg.get("indoor_roi")
    outdoor_roi = cfg.get("outdoor_roi")

    if indoor_roi is None or outdoor_roi is None:
        log.error("ROIs not configured. Run with --calibrate first.")
        return None, None

    x, y, w, h = indoor_roi
    indoor_crop = img.crop((x, y, x + w, y + h))
    indoor_temp, _ = read_temperature(indoor_crop, "indoor")

    x, y, w, h = outdoor_roi
    outdoor_crop = img.crop((x, y, x + w, y + h))
    outdoor_temp, _ = read_temperature(outdoor_crop, "outdoor")

    return indoor_temp, outdoor_temp


def monitor_loop(cfg):
    """Main polling loop."""
    title = cfg["window_title"]
    interval = cfg.get("polling_interval_seconds", 60)
    margin = cfg.get("alert_margin", 2.0)
    beep_enabled = cfg.get("beep_enabled", True)
    csv_path = cfg.get("csv_log_path", "nest_readings.csv")

    alert_active = False

    log.info("Monitor started - polling every %d s", interval)
    log.info("Window title match: '%s'", title)
    log.info("Alert margin: %.1f C", margin)
    log.info("Press Ctrl+C to quit (alarm beeps until temps change)")
    print()

    # Startup sanity check: make sure the target window exists
    test_window = find_chrome_window(title)
    if test_window is None:
        print()
        print("=" * 70)
        print("  ERROR: No Chrome window matching '%s' was found." % title)
        print()
        print("  To get started:")
        print("    1. Open Chrome and go to home.nest.com")
        print("    2. Log in to your Nest account")
        print("    3. Keep the window visible (not minimised)")
        print("    4. Run this script again")
        print()
        print("  Or run calibration to pick the window interactively:")
        print("    python nest_window_warner.py --calibrate")
        print()
        print("  To see all visible window titles:")
        print("    python nest_window_warner.py --list-windows")
        print("=" * 70)
        print()
        sys.exit(1)

    while True:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Capture
        img, window = capture_window(title, cfg)
        if img is None:
            log.warning("%s | Window not found or capture failed", ts)
            time.sleep(interval)
            continue

        # OCR
        indoor, outdoor = read_temperatures(img, cfg)

        if indoor is None or outdoor is None:
            status = "OCR_FAIL"
            # Distinguish which sensor failed
            in_str = f"{indoor:.1f}" if indoor is not None else "ERR"
            out_str = f"{outdoor:.1f}" if outdoor is not None else "ERR"
            print(f"{ts} | indoor={in_str} | outdoor={out_str} | status={status}")
            append_csv(csv_path, ts, in_str, out_str, status)
        else:
            # Determine status
            if outdoor >= indoor - margin:
                status = "CLOSE"
            else:
                status = "OPEN"

            print(
                f"{ts} | indoor={indoor:.1f} | outdoor={outdoor:.1f} | status={status}"
            )
            append_csv(csv_path, ts, indoor, outdoor, status)

            # Alert logic
            if beep_enabled:
                if status == "CLOSE" and not alert_active:
                    start_alert(cfg)
                    alert_active = True
                elif status == "OPEN" and alert_active:
                    stop_alert()
                    alert_active = False

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Test-image mode
# ---------------------------------------------------------------------------
def test_image_mode(image_path, cfg):
    """Run OCR on a static screenshot file and print results."""
    from PIL import Image

    if not os.path.exists(image_path):
        log.error("File not found: %s", image_path)
        return

    img = Image.open(image_path)
    log.info("Testing OCR on: %s  (%dx%d)", image_path, img.width, img.height)

    indoor, outdoor = read_temperatures(img, cfg)

    if indoor is not None and outdoor is not None:
        margin = cfg.get("alert_margin", 2.0)
        status = "CLOSE" if outdoor >= indoor - margin else "OPEN"
        print(f"indoor={indoor:.1f} | outdoor={outdoor:.1f} | status={status}")
    else:
        print("OCR failed on one or both ROIs.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Nest Temperature Monitor - OCR-based Chrome window monitor"
    )
    parser.add_argument(
        "--calibrate",
        nargs="?",
        const="__live__",
        default=None,
        metavar="IMAGE",
        help="Launch calibration GUI. Pass an optional image path to calibrate from a static screenshot instead of live capture.",
    )
    parser.add_argument(
        "--list-windows",
        action="store_true",
        help="List all visible window titles and exit",
    )
    parser.add_argument(
        "--test-image",
        type=str,
        metavar="PATH",
        help="Test OCR on a static screenshot without live capture",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config JSON (default: ./config.json)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override polling interval in seconds",
    )
    parser.add_argument(
        "--window-title",
        type=str,
        default=None,
        help="Override window title substring to match",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=None,
        help="Override alert margin (outdoor >= indoor - margin triggers alert)",
    )
    parser.add_argument(
        "--no-beep",
        action="store_true",
        help="Disable audible alerts",
    )
    parser.add_argument(
        "--capture-method",
        type=str,
        choices=["auto", "print_window", "screen_crop", "foreground_crop"],
        default=None,
        help="Capture strategy: auto (background first, then fallbacks), "
             "print_window (background), screen_crop (desktop crop), foreground_crop (activate window)",
    )
    parser.add_argument(
        "--debug-capture",
        action="store_true",
        help="Save each capture to disk as a PNG for troubleshooting",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Disable automatic window restore when minimised",
    )
    parser.add_argument(
        "--maximize-on-restore",
        action="store_true",
        help="Maximise window after restoring from minimised state",
    )
    parser.add_argument(
        "--beep-duration",
        type=int,
        default=None,
        metavar="MS",
        help="Beep duration in milliseconds (default: 800). Longer beeps survive Dolby Atmos audio lag.",
    )
    parser.add_argument(
        "--beep-interval",
        type=float,
        default=None,
        metavar="SEC",
        help="Seconds between beeps during alert (default: 1.5)",
    )
    args = parser.parse_args()

    # Load config
    config_path = args.config if args.config else CONFIG_PATH
    cfg = load_config(config_path)

    # CLI overrides
    if args.interval is not None:
        cfg["polling_interval_seconds"] = args.interval
    if args.window_title is not None:
        cfg["window_title"] = args.window_title
    if args.margin is not None:
        cfg["alert_margin"] = args.margin
    if args.no_beep:
        cfg["beep_enabled"] = False
    if args.capture_method is not None:
        cfg["capture_method"] = args.capture_method
    if args.debug_capture:
        cfg["debug_capture"] = True
    if args.no_restore:
        cfg["restore_before_capture"] = False
    if args.maximize_on_restore:
        cfg["maximize_on_restore"] = True
    if args.beep_duration is not None:
        cfg["beep_duration_ms"] = args.beep_duration
    if args.beep_interval is not None:
        cfg["beep_interval_sec"] = args.beep_interval

    # --- --list-windows mode ---
    if args.list_windows:
        list_windows_command()
        return

    # Mode dispatch
    if args.test_image:
        test_image_mode(args.test_image, cfg)
        return

    if args.calibrate is not None:
        img = None

        # If the user passed an explicit image path, use it directly
        if args.calibrate != "__live__":
            image_path = args.calibrate
            if not os.path.exists(image_path):
                log.error("File not found: %s", image_path)
                sys.exit(1)
            from PIL import Image
            img = Image.open(image_path)
            log.info("Calibrating from static image: %s  (%dx%d)", image_path, img.width, img.height)
        else:
            # No image path - offer live capture or manual screenshot
            print()
            print("  Calibration Mode")
            print("  ----------------")
            print("  [1]  Live capture from a visible Chrome/Nest window")
            print("  [2]  Calibrate from a saved screenshot image")
            print()

            while True:
                try:
                    mode = input("  Choose [1] or [2] (or 0 to cancel): ").strip()
                    if mode == "0":
                        log.info("Calibration cancelled.")
                        sys.exit(0)
                    if mode == "1":
                        break  # proceed to live capture
                    if mode == "2":
                        path = input("  Path to screenshot image: ").strip()
                        if not os.path.exists(path):
                            print(f"  File not found: {path}")
                            continue
                        from PIL import Image
                        img = Image.open(path)
                        log.info("Calibrating from static image: %s  (%dx%d)", path, img.width, img.height)
                        break
                    print("  Please enter 1, 2, or 0.")
                except (EOFError, KeyboardInterrupt):
                    log.info("Calibration cancelled.")
                    sys.exit(0)

            # --- Live capture path (mode 1) ---
            if img is None:
                title = cfg["window_title"]
                log.info("Select the window showing your Nest page:")

                # Interactive window selection (always shows the list)
                window, chosen_title = pick_window_interactive(title)
                if window is None:
                    log.error("No window selected. Calibration cancelled.")
                    sys.exit(1)

                # Save the chosen window title to config so future runs use it
                if chosen_title != cfg.get("window_title"):
                    cfg["window_title"] = chosen_title
                    save_config(cfg, config_path)

                method = cfg.get("capture_method", "auto")
                debug = cfg.get("debug_capture", False)
                restore = cfg.get("restore_before_capture", True)
                maximize = cfg.get("maximize_on_restore", False)
                img = capture_window_region(window, capture_method=method,
                                            debug_capture=debug,
                                            restore_before_capture=restore,
                                            maximize_on_restore=maximize)
                if img is None:
                    log.error(
                        "Could not capture window '%s'. "
                        "Make sure it is visible and not minimised.\n"
                        "  Tip: try '--calibrate screenshot.png' instead "
                        "using a manual screenshot.",
                        chosen_title,
                    )
                    sys.exit(1)

        log.info("Launching calibration GUI. Draw ROIs for indoor & outdoor temps.")
        app = CalibrationApp(img, config_path)
        app.run()
        log.info("Calibration complete.")
        return

    # Normal monitor mode
    if cfg["indoor_roi"] is None or cfg["outdoor_roi"] is None:
        log.error(
            "ROIs not configured. Run with --calibrate first to select "
            "indoor and outdoor temperature regions."
        )
        sys.exit(1)

    # Graceful shutdown on Ctrl+C
    def shutdown(signum, frame):
        log.info("Shutting down ...")
        stop_alert()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    monitor_loop(cfg)


if __name__ == "__main__":
    main()
