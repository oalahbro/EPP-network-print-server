# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EPP (Eclipse Print Protocol) is a Windows-only network print server for thermal printers using ESC/POS encoding. It acts as a CUPS-like print spooler for Windows, receiving raw print data over a TCP socket and forwarding it to a Windows printer via `win32print`. It includes a Flask web dashboard for configuration, log viewing, and receipt reprinting.

**Windows-only**: depends on `win32print` (pywin32) and `os.environ['COMPUTERNAME']`.

## Running the Application

```bash
pip install flask waitress pywin32 pillow pystray
python epp.py
```

The app can also be packaged with PyInstaller (see `get_resource_path()` for `_MEIPASS` support).

## Architecture

The entire application lives in `epp.py` — a single-file server with three concurrent components started in `__main__`:

1. **Socket server** (`start_server`): Listens on configurable port (default 9100), receives raw bytes from clients, detects ESC/POS vs generic data, and sends to printer via `send_to_printer()`.
2. **Flask web dashboard** (`app`): Served by Waitress on port 5000. Provides printer selection, port/reprint config, log viewer, print history with reprint and receipt preview.
3. **System tray icon** (`run_tray`): Uses `pystray` to show a tray icon with a quit option. Runs on the main thread; the other two run as daemon threads.

### Key data flow

- Clients send raw ESC/POS bytes over TCP -> socket server -> `send_to_printer()` -> `win32print` API
- Print jobs are saved to `print_history.json` with raw data stored as hex strings
- Reprints go through the same `send_to_printer()` path with a reprint counter and ESC/POS "REPRINT" header prepended
- Config changes via the dashboard trigger an app restart (`os.execl`)

### Configuration

`conf.json` stores: `DEFAULT` (printer short name), `PRINTER_NAME` (full UNC path like `\\COMPUTER\PRINTER`), `PORT` (socket port), `FLASK_PORT`, `MAX_REPRINT`.

### File layout

- `epp.py` — all server logic (socket server, Flask routes, printer interaction, tray icon)
- `conf.json` — runtime configuration (auto-created with defaults if missing)
- `print_history.json` — print job history (raw data as hex, limited to 500 entries)
- `templates/dashboard.html` — Jinja2 template for the main dashboard
- `templates/restart.html` — redirect page shown after config changes
- `static/main.js` — client-side JS for log refresh, tab switching, reprint/view actions, emoji fix
- `static/style.css` — dashboard styles
- `static/icon.png` — tray/favicon icon

### Flask routes

- `GET/POST /` — dashboard (GET renders, POST saves config and redirects to restart)
- `POST /reprint/<job_id>` — reprint a historical job
- `GET /view/<job_id>` — view raw receipt data for a job
- `GET /restart` — restart notification page

### Logging

Uses `RotatingFileHandler` writing to `server_log.txt` with 5MB rotation and 5 backups. Log files are gitignored (`.txt`, `.bak`).

## Language

Code comments and log messages are in Indonesian (Bahasa Indonesia). The README is also in Indonesian.
