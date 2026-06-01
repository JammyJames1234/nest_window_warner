# Nest Window Warner

Monitors Google Nest indoor/outdoor temperatures by OCR-ing a Chrome window
that shows the Nest dashboard. Alerts with a beep when the outdoor temperature
approaches or exceeds the indoor temperature — telling you when to **close**
your windows (or when it's safe to open them).

## Setup (Windows)

```bash
# 1. Install dependencies
pip install pygetwindow pillow opencv-python easyocr

# 2. Open the Nest page in Chrome (e.g. home.nest.com or the Google Home web app).
#    Keep the window visible on screen — it does NOT need to be focused.

# 3. (Optional) List all visible windows to find yours:
python nest_window_warner.py --list-windows

# 4. Calibrate — two options:

#  Option A: Live capture
python nest_window_warner.py --calibrate
#    → interactive window picker → draw ROIs on the live screenshot

#  Option B: Calibrate from a manual screenshot (useful for 4K/DPI issues)
#    Take a screenshot with Win+Shift+S or PrintScreen, save it, then:
python nest_window_warner.py --calibrate nest_screenshot.png
#    → skips window capture → draw ROIs directly on the image

# 5. Run the monitor loop
python nest_window_warner.py
```

**4K / High-DPI displays:** The script uses `DwmGetWindowAttribute` (Windows DWM)
to get the window's physical pixel coordinates, then grabs the full screen and crops.
This bypasses DPI scaling entirely — calibration ROIs should be pixel-accurate.

If the DWM approach fails (rare), the script falls back to a foreground capture
that briefly activates the window. You can control this behaviour with
`--capture-method` (see below).

**Window selection:** The calibration step includes an interactive window picker:
- If `window_title` doesn't match, you get a numbered list of ALL visible windows.
- If multiple match, matches are highlighted.
- Pick by number; the title is saved to `config.json`.
- Use `--window-title "home - Nest"` to set it directly.
- The window is captured silently — it won't steal focus.

## Usage

```
python nest_window_warner.py [options]

Options:
  --calibrate [IMAGE]  Launch ROI selection GUI. With no arg: live capture.
                       With image path: calibrate from a static screenshot.
  --list-windows       Print all visible window titles and exit
  --test-image PATH    Test OCR on a static screenshot
  --config PATH        Path to config JSON (default: ./config.json)
  --interval SECONDS   Override polling interval
  --window-title TEXT  Override Chrome window title substring to match
  --margin DEGREES     Override alert margin
  --no-beep            Disable audible alerts
  --capture-method M   Capture strategy: auto (default), print_window,
                       screen_crop, or foreground_crop.  See "Capture Methods" below.
  --beep-duration MS   Beep duration in milliseconds (default: 800).
                       Longer beeps survive Dolby Atmos audio wake-up lag.
  --beep-interval SEC  Seconds between beeps during alert (default: 1.5).
  --no-restore         Disable automatic window restore when minimised.
  --maximize-on-restore  Maximise window after restoring from minimised state.
  --debug-capture      Save every capture as a debug PNG to disk
```

## How It Works

1. Every N seconds (default 60), the script captures a screenshot of the Chrome
   window matching the configured title (without activating/focusing it).
2. Two pre-configured regions of interest (ROIs) are cropped from the screenshot.
3. Each crop is preprocessed (grayscale, 3× enlarge, CLAHE contrast, OTSU
   threshold) and fed to EasyOCR.
4. Numeric temperature values are extracted and checked for plausibility.
5. The status is **OPEN** when `outdoor < indoor - margin`, otherwise **CLOSE**.
6. When CLOSE, a repeating beep sounds until the user presses **Enter** to
   acknowledge, or the status returns to OPEN.
7. All readings are appended to a CSV file for later analysis.

## Config File (`config.json`)

```json
{
  "window_title": "Nest",
  "polling_interval_seconds": 60,
  "alert_margin": 2.0,
  "beep_enabled": true,
  "indoor_roi": [120, 85, 60, 28],
  "outdoor_roi": [440, 85, 60, 28],
  "csv_log_path": "nest_readings.csv"
}
```

| Key | Description |
|---|---|
| `window_title` | Substring of the Chrome window title to match |
| `polling_interval_seconds` | Seconds between readings |
| `alert_margin` | Alert when `outdoor >= indoor - margin` |
| `beep_enabled` | Enable/disable audible alert |
| `indoor_roi` | `[x, y, width, height]` of indoor temperature |
| `outdoor_roi` | `[x, y, width, height]` of outdoor temperature |
| `csv_log_path` | Path to append CSV readings |
| `capture_method` | `auto`, `print_window`, `screen_crop`, or `foreground_crop` |
| `beep_duration_ms` | Duration of each alert beep in milliseconds |
| `beep_interval_sec` | Seconds between consecutive beeps during alert |
| `restore_before_capture` | If `true`, auto-restore minimised windows before capture |
| `maximize_on_restore` | If `true`, maximise window after restoring it |
| `debug_capture` | If `true`, save debug screenshots on each capture |

## Capture Methods

The script offers four capture strategies, configurable via `--capture-method`
or the `capture_method` key in `config.json`:

| Method | Description | Focus Lost? |
|---|---|---|
| `auto` (default) | Try `print_window` first, then `screen_crop`, then `foreground_crop` | Usually no |
| `print_window` | Ask Windows to render the window off-screen (background capture) | No |
| `screen_crop` | Query DWM for physical window bounds → full-screen grab → crop | No |
| `foreground_crop` | Activate the window → brief sleep → full-screen grab → crop | Yes (briefly) |

- **`print_window`** is the least intrusive — it captures the window even when
  it's behind other windows. Best for most setups.
- **`screen_crop`** is accurate on all DPI settings and doesn't steal focus.
  Useful if `print_window` renders a blank image (e.g. some GPU-accelerated apps).
- **`foreground_crop`** is the most compatible but steals focus briefly.
  Useful as a last resort.
- **`auto`** tries the above in order and uses whatever works.

To debug capture issues, run with `--debug-capture` — each captured image
is saved as `debug_<tag>_<timestamp>.png` in the script directory.

## Testing OCR Without Live Capture

```bash
# Take a manual screenshot of the Nest window and save it
python nest_window_warner.py --test-image nest_screenshot.png
```

This runs the full OCR pipeline on a static image so you can verify ROI
placement and OCR accuracy before starting the monitor loop.

## Tips

- The Nest window must be **visible** (not minimised or fully covered), but
  does **not** need to be focused — the script won't steal your focus.
- If OCR frequently fails, re-run `--calibrate` and draw slightly larger ROIs
  around the temperature digits.
- Adjust `--margin` based on your preference (e.g. `--margin 0` alerts when
  outdoor reaches indoor, `--margin 5` alerts when outdoor is within 5°C of
  indoor).
- On 4K/High-DPI displays, if live capture coordinates seem off, calibrate from
  a manual screenshot: `python nest_window_warner.py --calibrate screenshot.png`

## Dependencies

- Python 3.11+
- `pygetwindow` — window finding
- `Pillow` — screenshot capture and image handling
- `opencv-python` — image preprocessing
- `easyocr` — OCR engine
- `winsound` (Windows built-in) — audible beep
