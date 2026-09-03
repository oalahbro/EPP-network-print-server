import os
import sys
import socket
import win32print
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
import pystray
import shutil
import re
from pystray import MenuItem as item, Menu
import io
import base64
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, jsonify, render_template, request, redirect, url_for
from waitress import serve
from datetime import datetime, timedelta
import webbrowser
import win32serviceutil
import win32service
import win32event
import win32api
import winerror
import servicemanager

from escpos import (
    decode_gs_character_size,
    decode_text,
    parse_stream,
    replace_cuts,
    text_is_non_ascii,
)
from legacy_renderer import (
    LegacyProfile,
    UnsupportedLegacyObject,
    render_legacy_receipts,
)

RASTER_FONT_SIZE_MIN = 12
RASTER_FONT_SIZE_MAX = 48
RASTER_MAX_WIDTH_MIN = 192
RASTER_MAX_WIDTH_MAX = 1200


def _parse_bounded_int(value, field_name, minimum, maximum):
    """Parse a dashboard integer and enforce its server-side bounds."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} harus berupa angka.")
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name} harus berada di antara {minimum} dan {maximum}.")
    return parsed


def validate_raster_settings(font_size, max_width):
    """Validate raster settings before they are persisted."""
    return {
        "RASTER_FONT_SIZE": _parse_bounded_int(
            font_size, "Ukuran font", RASTER_FONT_SIZE_MIN, RASTER_FONT_SIZE_MAX
        ),
        "RASTER_MAX_WIDTH": _parse_bounded_int(
            max_width, "Lebar raster", RASTER_MAX_WIDTH_MIN, RASTER_MAX_WIDTH_MAX
        ),
    }


# Optional: unidecode for fallback transliteration
try:
    from unidecode import unidecode
    HAS_UNIDECODE = True
except ImportError:
    HAS_UNIDECODE = False
    def unidecode(text):
        return text

# App directory (absolute path)
def get_app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")

APP_DIR = get_app_dir()

# Konfigurasi (absolute paths)
CONFIG_FILE = os.path.join(APP_DIR, "conf.json")
LOG_FILE = os.path.join(APP_DIR, "server_log.txt")
PRINT_HISTORY_FILE = os.path.join(APP_DIR, "print_history.json")
HISTORY_DIR = os.path.join(APP_DIR, "history")
BACKUP_RETENTION_DAYS = 7
BACKUP_FILE_PATTERN = re.compile(r"^print_history_(\d{4}-\d{2}-\d{2})\.json$")
LOG_MAX_SIZE = 5 * 1024 * 1024
DEFAULT_PORT = 9100
FLASK_PORT = 5000
MAX_REPRINT = 3
HOST = "0.0.0.0"
BUFFER_SIZE = 2048

stop_event = threading.Event()
_last_rotation_check_date = None
_history_lock = threading.RLock()


def _atomic_write_json(path, data, indent=4):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _quarantine_corrupt_file(path, reason):
    dst = f"{path}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, dst)
        logging.error(f"⚠️ Corrupt JSON quarantined -> {dst}: {reason}")
    except OSError as e:
        logging.error(f"❌ Gagal quarantine {path}: {e}")
    return dst


def _safe_load_json(path, default, *, recreate=False):
    import copy
    if not os.path.exists(path):
        if recreate:
            try:
                _atomic_write_json(path, default)
            except OSError as e:
                logging.error(f"❌ Gagal inisialisasi {path}: {e}")
        return copy.deepcopy(default)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        _quarantine_corrupt_file(path, str(e))
        if recreate:
            try:
                _atomic_write_json(path, default)
            except OSError as we:
                logging.error(f"❌ Gagal reinit {path}: {we}")
        return copy.deepcopy(default)


def get_resource_path(relative_path):
    """Dapatkan path file dalam aplikasi PyInstaller."""
    if getattr(sys, 'frozen', False):  # Jika aplikasi sudah di-build
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def ensure_icon_available():
    ICON_PATH = get_resource_path("static/icon.png")  # Ambil ikon dari folder yang benar

    # Simpan ikon di folder yang aman (%APPDATA%)
    temp_dir = os.path.join(os.getenv("APPDATA"), "PrintServer")
    os.makedirs(temp_dir, exist_ok=True)
    temp_icon_path = os.path.join(temp_dir, "icon.png")

    # Salin ikon jika belum ada atau berbeda
    if not os.path.exists(temp_icon_path) or not file_is_same(ICON_PATH, temp_icon_path):
        shutil.copy(ICON_PATH, temp_icon_path)

    return temp_icon_path

def file_is_same(src, dst):
    """Cek apakah dua file sama berdasarkan ukuran."""
    return os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst)

# Flask Web Dashboard
app = Flask(__name__)
status = {"last_request": None, "total_jobs": 0, "last_error": None}

# queue


# Configuration helpers are defined below, after the raster helpers they use.
def ensure_history_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)

def get_backup_filepath(date_str):
    return os.path.join(HISTORY_DIR, f"print_history_{date_str}.json")

def _entry_date(entry):
    ts = entry.get("timestamp") if isinstance(entry, dict) else None
    if not ts or len(ts) < 10:
        return None
    try:
        datetime.strptime(ts[:10], "%Y-%m-%d")
        return ts[:10]
    except ValueError:
        return None

def rotate_history_if_needed():
    with _history_lock:
        ensure_history_dir()

        if not os.path.exists(PRINT_HISTORY_FILE):
            return

        history = _safe_load_json(PRINT_HISTORY_FILE, [], recreate=False)

        if not history:
            return

        today = datetime.now().strftime("%Y-%m-%d")
        today_entries = []
        archive_groups = {}
        for entry in history:
            d = _entry_date(entry)
            if d is None or d == today:
                today_entries.append(entry)
            else:
                archive_groups.setdefault(d, []).append(entry)

        if not archive_groups:
            return

        for date_str, entries in archive_groups.items():
            path = get_backup_filepath(date_str)
            existing = _safe_load_json(path, [], recreate=False)
            merged = entries + existing
            _atomic_write_json(path, merged)
            logging.info(f"📦 History {date_str} diarsipkan ({len(entries)} entry baru, total {len(merged)})")

        _atomic_write_json(PRINT_HISTORY_FILE, today_entries)
        logging.info(f"🔄 History aktif direset, sisa {len(today_entries)} entry hari ini")

def cleanup_old_backups():
    if not os.path.isdir(HISTORY_DIR):
        return
    today = datetime.now().date()
    try:
        for name in os.listdir(HISTORY_DIR):
            m = BACKUP_FILE_PATTERN.match(name)
            if not m:
                continue
            try:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - file_date).days > BACKUP_RETENTION_DAYS:
                path = os.path.join(HISTORY_DIR, name)
                try:
                    os.remove(path)
                    logging.info(f"🗑️ Arsip lama dihapus: {name}")
                except OSError as e:
                    logging.error(f"❌ Gagal hapus arsip {name}: {e}")
    except OSError as e:
        logging.error(f"❌ Gagal scan folder arsip: {e}")

def load_backup_history(date_str):
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return []
    path = get_backup_filepath(date_str)
    if not os.path.exists(path):
        return []
    return _safe_load_json(path, [], recreate=False)

def list_available_backup_dates():
    if not os.path.isdir(HISTORY_DIR):
        return []
    dates = []
    try:
        for name in os.listdir(HISTORY_DIR):
            m = BACKUP_FILE_PATTERN.match(name)
            if m:
                dates.append(m.group(1))
    except OSError:
        return []
    dates.sort(reverse=True)
    return dates[:BACKUP_RETENTION_DAYS]

def load_print_history():
    global _last_rotation_check_date
    with _history_lock:
        today = datetime.now().strftime("%Y-%m-%d")
        if _last_rotation_check_date != today:
            try:
                rotate_history_if_needed()
                cleanup_old_backups()
            except Exception as e:
                logging.error(f"❌ Rotate/cleanup history gagal: {e}")
            _last_rotation_check_date = today

        return _safe_load_json(PRINT_HISTORY_FILE, [], recreate=True)

def save_print_history(history):
    with _history_lock:
        _atomic_write_json(PRINT_HISTORY_FILE, history)

def modify_cut_command(data, cut_mode):
    """Modify only parsed ESC/POS cut commands, never image payload bytes."""
    if cut_mode == "default":
        return data
    return replace_cuts(data, cut_mode)  # escpos.py: lossless token walk


def _stream_contains_valid_image(data):
    """Return True only when the stream has a complete recognized image token."""
    try:
        return any(token.kind == "image" for token in parse_stream(data))
    except ValueError:
        return False


def _stream_has_unsupported_text(data):
    """Detect high bytes only in text tokens, not in image/binary payloads."""
    try:
        return any(text_is_non_ascii(token) for token in parse_stream(data))
    except ValueError:
        return False


def _render_legacy_full_raster(source_data, config):
    """Render legacy receipts while preserving each receipt's cut boundary."""
    profile = LegacyProfile(
        width_dots=int(config.get("RASTER_MAX_WIDTH", 384)),
        columns=32,
        font_size=int(config.get("RASTER_FONT_SIZE", 20)),
        line_spacing=1.0,
        margin_dots=0,
        image_scale_to_width=True,
        image_invert=bool(config.get("LEGACY_IMAGE_INVERT", False)),
    )
    font = _get_font(profile.font_size, config.get("RASTER_FONT", "") or None)
    pages = render_legacy_receipts(source_data, font, profile)
    output = bytearray()
    for rendered, cut in pages:
        if rendered is not None:
            width_bytes, height, bitmap = rendered
            output.extend(_build_raster_command(width_bytes, height, bitmap))
        if cut:
            output.extend(cut)
    return bytes(output)


DEFAULT_CONFIG = {
    "DEFAULT": "",
    "PRINTER_NAME": "",
    "PORT": DEFAULT_PORT,
    "FLASK_PORT": FLASK_PORT,
    "MAX_REPRINT": MAX_REPRINT,
    "CUT_MODE": "default",
    "PRINT_MODE": "native",
    "RASTER_FONT_SIZE": 24,
    "RASTER_MAX_WIDTH": 576,
    "RASTER_FONT": "",
}


def _normalize_raster_config(config):
    """Return safe in-memory raster settings for an existing config."""
    defaults = {
        "RASTER_FONT_SIZE": DEFAULT_CONFIG["RASTER_FONT_SIZE"],
        "RASTER_MAX_WIDTH": DEFAULT_CONFIG["RASTER_MAX_WIDTH"],
    }
    for key, default in defaults.items():
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            value = default
        if key == "RASTER_FONT_SIZE":
            valid = RASTER_FONT_SIZE_MIN <= value <= RASTER_FONT_SIZE_MAX
        else:
            valid = RASTER_MAX_WIDTH_MIN <= value <= RASTER_MAX_WIDTH_MAX
        config[key] = value if valid else default
    config.setdefault("PRINT_MODE", DEFAULT_CONFIG["PRINT_MODE"])
    config.setdefault("RASTER_FONT", DEFAULT_CONFIG["RASTER_FONT"])
    return config


def _render_dashboard(config, printers, history, error_message=None, status_data=None):
    """Render dashboard data shared by normal and validation-error responses."""
    logs = read_log()
    backup_dates = list_available_backup_dates()
    today_str = datetime.now().strftime("%Y-%m-%d")
    printer_status = status_data if status_data is not None else check_printer_status()
    return render_template(
        "dashboard.html",
        status=status,
        config=config,
        logs=logs,
        printers=printers,
        default_printer=config.get("DEFAULT", ""),
        history=history,
        backup_dates=backup_dates,
        today_str=today_str,
        printer_status=printer_status,
        error_message=error_message,
    )


def _config_with_defaults(config):
    if not isinstance(config, dict):
        config = {}
    return _normalize_raster_config({**DEFAULT_CONFIG, **config})


def load_config():
    if not os.path.exists(CONFIG_FILE):
        config = dict(DEFAULT_CONFIG)
        _atomic_write_json(CONFIG_FILE, config)
        return config

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logging.error("❌ Config tidak valid, memakai default aman: %s", exc)
        config = {}

    return _config_with_defaults(config)


def save_config(config):
    _atomic_write_json(CONFIG_FILE, _config_with_defaults(dict(config)))


# queue


def _render_hybrid_text(source_data, config):
    """Rasterize only unsupported text tokens and preserve all other bytes."""
    try:
        tokens = parse_stream(source_data)
    except ValueError:
        return source_data, "hybrid-parse-fallback"

    renderer_state = ESCPOSRenderer(
        int(config.get("RASTER_FONT_SIZE", 24)),
        int(config.get("RASTER_MAX_WIDTH", 384)),
        config.get("RASTER_FONT", "") or None,
        1.0,
    )
    output = bytearray()
    converted = 0

    for token in tokens:
        if token.kind == "text" and text_is_non_ascii(token):
            text = decode_text(token.raw)
            if not text:
                output.extend(token.raw)
                continue

            # Snapshot formatting before consuming the text.  The line feed
            # that follows remains in the original stream and advances paper.
            line_renderer = ESCPOSRenderer(
                renderer_state.font_size,
                renderer_state.max_width,
                renderer_state.font_path,
                renderer_state.line_spacing,
            )
            line_renderer.current_format = dict(renderer_state.current_format)
            line_renderer.add_text(text)
            rendered = line_renderer.render()
            if rendered[0] is None:
                output.extend(token.raw)
            else:
                output.extend(_build_raster_command(*rendered))
                converted += 1
        else:
            output.extend(token.raw)

        if token.kind in ("control", "feed"):
            renderer_state.add_control(token.raw)

    return bytes(output), "hybrid" if converted else "native"


def prepare_print_data(source_data, config):
    """Return (printer_data, mode), preferring byte-preserving native output."""
    mode = str(config.get("PRINT_MODE", "native")).strip().lower()
    if mode in ("", "auto"):
        mode = "native"

    # Native is the safe default: it preserves the producer's character
    # metrics, images, polarity, feeds, and command ordering exactly.
    if mode == "native":
        return source_data, "native"

    if mode == "hybrid":
        return _render_hybrid_text(source_data, config)

    if mode == "legacy_full_raster":
        try:
            return _render_legacy_full_raster(source_data, config), "legacy-full-raster"
        except UnsupportedLegacyObject as exc:
            logging.warning("Legacy raster fallback to native: %s", exc)
            return source_data, "legacy-full-raster-fallback"
        except Exception as exc:
            logging.exception("Legacy full-raster conversion failed: %s", exc)
            return source_data, "legacy-full-raster-fallback"

    return source_data, "native-unknown-mode"


def add_reprint_mark(data, count):
    return (
        b"\x1b\x61\x01"              # center
        + b"\x1d\x21\x11"            # bold on
        + f"*** REPRINT ({count}) ***\n".encode()
        + b"\x1d\x21\x00"            # bold off
        + b"\x1b\x61\x00\n"          # left
        + data
    )

log_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=5, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(log_handler)
logging.getLogger("PIL").setLevel(logging.WARNING)  # Nonaktifkan log debug dari PIL (Pillow)

def send_to_printer(data, job_id=None):
    try:
        # Keep the incoming stream immutable for history and reprints.
        source_data = bytes(data)
        config = load_config()
        PRINTER_NAME = config.get("PRINTER_NAME", "")
        try:
            MAX_REPRINT = int(config.get("MAX_REPRINT", 0))
        except (TypeError, ValueError):
            logging.warning(f"⚠️ MAX_REPRINT di config tidak valid ({config.get('MAX_REPRINT')!r}), fallback ke 0")
            MAX_REPRINT = 0
        history = load_print_history()
        
        if not PRINTER_NAME:
            raise ValueError("Printer name not found in config.")
        
        if job_id is not None:
            job_found = None
            for job in history:
                if job["id"] == job_id:
                    job_found = job
                    break

            if not job_found:
                return {"status": False, "message": "Job not found"}

            # Reprints must be reconstructed from the original source when it
            # exists; old records fall back to their legacy raw_data field.
            source_data = bytes.fromhex(
                job_found.get("source_raw_data", job_found.get("raw_data", ""))
            )
            current_count = job_found.get("print_count", 0)
            data = source_data
            if not data:
                return {"status": False, "message": "Job has no source data"}

            # Cek max reprint
            if current_count >= MAX_REPRINT:
                logging.warning("❌ Max reprint reached")
                return {"status": False, "message": "Max reprint reached"}

                # Tambah counter
            current_count += 1
            job["print_count"] = current_count

            logging.info(f"🔁 Reprint Job ID: {job_id} (Count: {current_count})")
            logging.info(f"🖨️ Mengirim ke printer: {PRINTER_NAME}")

                # Tambahkan label REPRINT + count
            data = add_reprint_mark(data, current_count)
            save_print_history(history)
        else:
            logging.info("📃 Print job baru diterima")
            logging.info(f"🖨️ Mengirim ke printer: {PRINTER_NAME}")
        
        cut_mode = config.get("CUT_MODE", "default")
        if cut_mode != "default":
            data = modify_cut_command(data, cut_mode)

        # Prepare output only after source/reprint handling. Native mode is
        # intentionally byte-preserving; valid incoming images are never
        # mistaken for Unicode just because their pixels contain high bytes.
        output_data, conversion_mode = prepare_print_data(data, config)
        logging.info(
            "🧾 Print mode=%s source=%d bytes output=%d bytes",
            conversion_mode, len(data), len(output_data)
        )
        data = output_data

        hprinter = win32print.OpenPrinter(PRINTER_NAME)
        job_info = win32print.StartDocPrinter(hprinter, 1, ("Print Job EPP", None, "RAW"))
        win32print.StartPagePrinter(hprinter)
        win32print.WritePrinter(hprinter, data)
        win32print.EndPagePrinter(hprinter)
        win32print.EndDocPrinter(hprinter)
        win32print.ClosePrinter(hprinter)

        status["total_jobs"] += 1
        status["last_request"] = str(datetime.now())
        status["last_error"] = None
        logging.info("✅ Cetak berhasil.")
        logging.info("📤 Payload sent: %d bytes", len(data))

        if job_id is None:
            next_id = max((e.get("id", 0) for e in history), default=0) + 1
            job_entry = {
                "id": next_id,
                "printer": PRINTER_NAME,
                "timestamp": str(datetime.now()),
                # raw_data remains the original source for compatibility;
                # output_raw_data records the exact bytes sent to the printer.
                "size": len(source_data),
                "raw_data": source_data.hex(),
                "source_raw_data": source_data.hex(),
                "output_raw_data": data.hex(),
                "source_size": len(source_data),
                "output_size": len(data),
                "conversion_mode": conversion_mode,
                "print_count" : 0  # simpan dalam hex supaya aman di JSON
            }
            
            history.insert(0, job_entry)  # job terbaru di atas
            history = history[:500]
            save_print_history(history)
            logging.info(f"🧾 History tersimpan. Total job: {len(history)}")
    
        return {"status": True}
    except Exception as e:
        logging.error(f"❌ Kesalahan printer: {e}")
        status["last_error"] = {
            "message": str(e),
            "timestamp": str(datetime.now()),
            "job_id": job_id,
        }
        return {"status": False, "message": str(e)}

def check_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0

server_thread = None


def start_server():
    logging.info("🛠 Starting print server ...")
    config = load_config()
    port = config.get("PORT", DEFAULT_PORT)

    if check_port_in_use(port):
        logging.error(f"❌ Port {port} sudah digunakan!")
        return

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.settimeout(2.0)
            server.bind((HOST, port))
            server.listen(5)
            logging.info(f"🚀 Print server running on port {port}...")

            while not stop_event.is_set():
                try:
                    client, addr = server.accept()
                    logging.info(f"🔗 Connection received from {addr}")

                    with client:
                        try:
                            client.settimeout(2)  # Timeout untuk menerima data
                            data = b""  # Menyimpan data yang diterima

                            while True:
                                try:
                                    chunk = client.recv(BUFFER_SIZE)
                                    if not chunk:
                                        break  # Koneksi tertutup oleh client
                                    data += chunk
                                except socket.timeout:
                                    break  # Timeout, asumsi data selesai

                            if data:
                                if data.startswith(b"\x1b@"):
                                    logging.info("📃 Deteksi ESC/POS data (kasir)")
                                else:
                                    logging.info("📄 Deteksi dokumen non-ESC/POS (umum)")

                                logging.info(f"🖨 Mengirim {len(data)} bytes ke printer...")
                                send_to_printer(data)


                        except ConnectionResetError as e:
                            logging.warning(f"⚠️ Koneksi dengan {addr} terputus secara paksa: {e}")
                        except Exception as e:
                            logging.error(f"❌ Error tidak terduga saat menerima data dari {addr}: {e}")

                except socket.timeout:
                    continue
                except OSError as e:
                    if stop_event.is_set():
                        break
                    logging.error(f"❌ Error saat menerima koneksi: {e}")

            logging.info("🛑 Print server stopped.")

    except OSError as e:
        logging.error(f"❌ Gagal menjalankan server: {e}")


def restart_print_server():
    global server_thread
    stop_event.set()
    if server_thread and server_thread.is_alive():
        server_thread.join(timeout=5)
    stop_event.clear()
    import time
    time.sleep(1)
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    logging.info("🔄 Print server restarted with new config.")


def clean_log_text(text):
    """ Membersihkan karakter escape sequence dan merapikan teks log """
    text = re.sub(r'[\x1b\x1d][@\w]*', '', text)  # Hapus karakter escape seperti \x1b, \x1d
    text = text.replace("\n", "<br>").strip()  # Ubah \n jadi <br> untuk tampilan di HTML
    return text

def read_log():
    """ Membaca log dari file dan membersihkan encoding """
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            raw_logs = f.readlines()

        cleaned_logs = []
        for line in raw_logs:
            try:
                decoded_line = line.encode("utf-8").decode("unicode_escape")
                cleaned_logs.append(clean_log_text(decoded_line))
            except UnicodeDecodeError:
                cleaned_logs.append(clean_log_text(line))  # Gunakan raw text jika gagal decoding

        return cleaned_logs

    return []

def get_printer_list():
    printers = []
    try:
        for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_CONNECTIONS + win32print.PRINTER_ENUM_LOCAL):
            printers.append(printer[2])
    except Exception as e:
        logging.error(f"❌ Gagal enumerate printer (spooler down?): {e}")
    return printers


_PRINTER_STATUS_FLAGS = [
    (0x00000001, "Paused"),
    (0x00000002, "Error"),
    (0x00000004, "Pending Deletion"),
    (0x00000008, "Paper Jam"),
    (0x00000010, "Paper Out"),
    (0x00000040, "Paper Problem"),
    (0x00000080, "Offline"),
    (0x00000400, "Output Bin Full"),
    (0x00000800, "Not Available"),
    (0x00001000, "Waiting"),
    (0x00002000, "Processing"),
    (0x00004000, "Initializing"),
    (0x00008000, "Warming Up"),
    (0x00400000, "Door Open"),
    (0x00800000, "Server Unknown"),
]


def check_printer_status():
    """Probe current printer status via win32print. Returns dict or None on probe failure."""
    try:
        config = load_config()
        printer_name = config.get("PRINTER_NAME", "")
        if not printer_name:
            return {"ok": False, "labels": ["No printer configured"], "code": None,
                    "checked_at": str(datetime.now()), "printer": ""}
        hprinter = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(hprinter, 2)
        finally:
            win32print.ClosePrinter(hprinter)
        code = info.get("Status", 0) if isinstance(info, dict) else 0
        attrs = info.get("Attributes", 0) if isinstance(info, dict) else 0
        labels = [lbl for bit, lbl in _PRINTER_STATUS_FLAGS if code & bit]
        # PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400 — user toggle "Use Printer Offline"
        user_offline = bool(attrs & 0x400)
        if user_offline:
            labels.append("Offline (di-toggle manual: Use Printer Offline)")
        # Status flags yang bersifat "tidak ready" untuk thermal POS printer
        bad_mask = 0x00000001 | 0x00000002 | 0x00000008 | 0x00000010 | 0x00000040 | 0x00000080 | 0x00000800 | 0x00400000
        ok = (code & bad_mask) == 0 and not user_offline
        return {
            "ok": ok,
            "code": code,
            "attributes": attrs,
            "labels": labels if labels else (["Ready"] if ok else ["Unknown"]),
            "printer": printer_name,
            "checked_at": str(datetime.now()),
        }
    except Exception as e:
        msg = str(e)
        # Windows error 1722: "The RPC server is unavailable" — Print Spooler service mati
        if "1722" in msg or "RPC server is unavailable" in msg:
            label = "Print Spooler service mati — start service 'Spooler' di Windows Services"
        else:
            label = f"Probe gagal: {e}"
        return {"ok": False, "labels": [label], "code": None,
                "printer": "", "checked_at": str(datetime.now())}

@app.route("/", methods=["GET", "POST"])
def dashboard():
    config = load_config()
    printers = get_printer_list()
    history = load_print_history()

    if request.method == "POST":
        new_default = request.form.get("default_printer", "").strip()
        new_port = request.form.get("port", "").strip()
        new_maxreprint = request.form.get("max_reprint", "").strip()
        new_cut_mode = request.form.get("cut_mode", "").strip()
        new_print_mode = request.form.get("print_mode", "native").strip().lower()
        new_raster_font = request.form.get("raster_font", "").strip()
        try:
            raster_settings = validate_raster_settings(
                request.form.get("raster_font_size", ""),
                request.form.get("raster_max_width", ""),
            )
            new_port_value = int(new_port)
            new_maxreprint_value = int(new_maxreprint)
        except (TypeError, ValueError) as exc:
            candidate = dict(config)
            candidate.update({
                "DEFAULT": new_default,
                "PRINTER_NAME": new_default,
                "RASTER_FONT_SIZE": request.form.get("raster_font_size", ""),
                "RASTER_MAX_WIDTH": request.form.get("raster_max_width", ""),
            })
            return _render_dashboard(
                candidate, printers, history, str(exc), status_data={
                    "ok": False,
                    "labels": [str(exc)],
                    "code": None,
                    "printer": "",
                    "checked_at": str(datetime.now()),
                }
            ), 400

        candidate = dict(config)
        candidate["DEFAULT"] = new_default
        candidate["PRINTER_NAME"] = new_default
        candidate["PORT"] = new_port_value
        candidate["MAX_REPRINT"] = new_maxreprint_value
        candidate["CUT_MODE"] = new_cut_mode
        candidate["PRINT_MODE"] = new_print_mode if new_print_mode in ("native", "hybrid", "legacy_full_raster") else "native"
        candidate.update(raster_settings)
        candidate["RASTER_FONT"] = new_raster_font
        save_config(candidate)
        return redirect(url_for("restart_server"))

    return _render_dashboard(config, printers, history)


@app.route("/reprint/<int:job_id>", methods=["POST"])

def reprint(job_id):
    history = load_print_history()

    for job in history:
        if job["id"] == job_id:
            raw_hex = job.get("source_raw_data", job.get("raw_data", ""))
            raw_bytes = bytes.fromhex(raw_hex)
            result=send_to_printer(raw_bytes,job_id)

            if result["status"]:
                return {
                    "status": "success",
                    "message": "Reprint berhasil"
                }
            else:
                return {
                    "status": "error",
                    "message": result.get("message", "Reprint gagal")
                }, 400

    return {"status": "error", "message": "Job not found"}, 404


def raster_to_png(img_data, w_bytes, h):
    w = w_bytes * 8
    img = Image.new('1', (w, h), 1)
    pixels = img.load()
    for y in range(h):
        for xb in range(w_bytes):
            byte_val = img_data[y * w_bytes + xb]
            for bit in range(8):
                px = xb * 8 + bit
                if px < w:
                    pixels[px, y] = 0 if (byte_val >> (7 - bit)) & 1 else 1
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


def extract_escpos_images(raw_bytes):
    images = []
    i = 0
    while i < len(raw_bytes) - 7:
        # GS v 0 (1D 76 30) — standard Epson raster
        if raw_bytes[i] == 0x1D and raw_bytes[i+1] == 0x76 and raw_bytes[i+2] == 0x30:
            w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
            h = raw_bytes[i+6] + raw_bytes[i+7] * 256
            data_start = i + 8
            data_len = w_bytes * h

            if w_bytes > 0 and h > 0 and data_start + data_len <= len(raw_bytes):
                images.append(raster_to_png(raw_bytes[data_start:data_start + data_len], w_bytes, h))
                i = data_start + data_len
                continue

        # ESC GS S (1B 1D 53) — Star/Bixolon raster
        if (raw_bytes[i] == 0x1B and raw_bytes[i+1] == 0x1D and raw_bytes[i+2] == 0x53):
            w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
            h = raw_bytes[i+6] + raw_bytes[i+7] * 256
            data_start = i + 8
            data_len = w_bytes * h

            if w_bytes > 0 and h > 0 and data_start + data_len <= len(raw_bytes):
                images.append(raster_to_png(raw_bytes[data_start:data_start + data_len], w_bytes, h))
                i = data_start + data_len
                continue

        i += 1
    return images


def strip_escpos_images(raw_bytes):
    """Remove image data from ESC/POS, return text-only bytes."""
    result = bytearray()
    i = 0
    while i < len(raw_bytes):
        # GS v 0 (1D 76 30)
        if (i + 7 < len(raw_bytes) and
            raw_bytes[i] == 0x1D and raw_bytes[i+1] == 0x76 and raw_bytes[i+2] == 0x30):
            w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
            h = raw_bytes[i+6] + raw_bytes[i+7] * 256
            skip = 8 + w_bytes * h
            if w_bytes > 0 and h > 0 and i + skip <= len(raw_bytes):
                i += skip
                continue

        # ESC GS S (1B 1D 53)
        if (i + 7 < len(raw_bytes) and
            raw_bytes[i] == 0x1B and raw_bytes[i+1] == 0x1D and raw_bytes[i+2] == 0x53):
            w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
            h = raw_bytes[i+6] + raw_bytes[i+7] * 256
            skip = 8 + w_bytes * h
            if w_bytes > 0 and h > 0 and i + skip <= len(raw_bytes):
                i += skip
                continue

        result.append(raw_bytes[i])
        i += 1
    return bytes(result)


_FONT_CACHE = {}
_DEFAULT_FONT_PATH = None


def _find_font():
    """Find a font that supports CJK/Arabic. Prefer Noto Sans."""
    global _DEFAULT_FONT_PATH
    if _DEFAULT_FONT_PATH and os.path.exists(_DEFAULT_FONT_PATH):
        return _DEFAULT_FONT_PATH

    search_paths = [
        os.path.join(APP_DIR, "fonts", "NotoSans-Regular.ttf"),
        os.path.join(APP_DIR, "fonts", "NotoSansCJK-Regular.ttc"),
        os.path.join(APP_DIR, "fonts", "NotoSansArabic-Regular.ttf"),
        r"C:\Windows\Fonts\NotoSansCJK-Regular.ttc",
        r"C:\Windows\Fonts\NotoSansSC-Regular.otf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simfang.ttf",
        r"C:\Windows\Fonts\NotoSans-Regular.ttf",
        r"C:\Windows\Fonts\NotoSansArabic-Regular.ttf",
        r"C:\Windows\Fonts\msgothic.ttc",
        r"C:\Windows\Fonts\malgun.ttf",
        r"C:\Windows\Fonts\tradbdo.ttf",
        r"C:\Windows\Fonts\segoui.ttf",
        r"C:\Windows\Fonts\arialuni.ttf",
    ]

    for path in search_paths:
        if os.path.exists(path):
            _DEFAULT_FONT_PATH = path
            logging.info(f"Font found: {path}")
            return path

    logging.warning("No Unicode font found, will use PIL default (limited glyphs)")
    return None


def _get_font(size, font_path=None):
    """Get cached font at given size."""
    path = font_path or _find_font()
    key = (path, size)
    if key not in _FONT_CACHE:
        try:
            if path:
                _FONT_CACHE[key] = ImageFont.truetype(path, size)
            else:
                _FONT_CACHE[key] = ImageFont.load_default()
        except Exception as e:
            logging.warning(f"Font load failed: {e}, using default")
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _text_to_raster(text, font_size=24, max_width=576, font_path=None, line_spacing=1.15):
    """
    Render text to 1-bit bitmap (ESC/POS raster format).
    Returns (width_bytes, height, bitmap_bytes)
    """
    font = _get_font(font_size, font_path)

    # Split by newlines first
    lines = text.split('\n')

    # Handle wrapping for each line
    wrapped_lines = []
    dummy_img = Image.new('1', (1, 1))
    draw = ImageDraw.Draw(dummy_img)

    for line in lines:
        if not line:
            wrapped_lines.append('')
            continue

        # Calculate text size
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]

        if text_w > max_width:
            # Word wrap
            words = line.split(' ')
            current_line = ""
            for word in words:
                test_line = current_line + (" " if current_line else "") + word
                test_bbox = draw.textbbox((0, 0), test_line, font=font)
                if test_bbox[2] - test_bbox[0] <= max_width:
                    current_line = test_line
                else:
                    if current_line:
                        wrapped_lines.append(current_line)
                    current_line = word
            if current_line:
                wrapped_lines.append(current_line)
        else:
            wrapped_lines.append(line)

    # Calculate total height with configurable line spacing
    line_height = int(font_size * line_spacing)
    total_h = line_height * len(wrapped_lines)

    # Create bitmap (1-bit, white background)
    img = Image.new('1', (max_width, total_h), 1)  # 1 = white
    draw = ImageDraw.Draw(img)

    y = 0
    for line in wrapped_lines:
        draw.text((0, y), line, font=font, fill=0)  # 0 = black
        y += line_height

    # Convert to ESC/POS raster format (1 bit per pixel, MSB first)
    w_bytes = (max_width + 7) // 8
    bitmap = bytearray(w_bytes * total_h)

    pixels = img.load()
    for row in range(total_h):
        for col_byte in range(w_bytes):
            byte_val = 0
            for bit in range(8):
                x = col_byte * 8 + bit
                if x < max_width:
                    # PIL: 0=black, 1=white. ESC/POS: 1=black, 0=white
                    if pixels[x, row] == 0:
                        byte_val |= (0x80 >> bit)
            bitmap[row * w_bytes + col_byte] = byte_val

    return w_bytes, total_h, bytes(bitmap)


def _build_raster_command(w_bytes, height, bitmap_data):
    """Build ESC/POS GS v 0 raster command."""
    # GS v 0 m xL xH yL yH d1...dk
    # m=0 (normal), xL/xH = width in bytes, yL/yH = height
    xL = w_bytes & 0xFF
    xH = (w_bytes >> 8) & 0xFF
    yL = height & 0xFF
    yH = (height >> 8) & 0xFF
    header = bytes([0x1D, 0x76, 0x30, 0x00, xL, xH, yL, yH])
    return header + bitmap_data


def _get_escpos_command_length(cmd_byte, next_byte, third_byte=None):
    """
    Return length of ESC/POS command starting with cmd_byte (0x1B, 0x1D, 0x1C, 0x10).
    Returns 0 if unknown/not a command.
    """
    # ESC (0x1B) commands
    if cmd_byte == 0x1B:
        if next_byte is None:
            return 0
        # 2-byte commands without a parameter
        if next_byte in (0x40, 0x32, 0x57, 0x4A, 0x4B, 0x5C, 0x7B, 0x7D):
            return 2
        # 3-byte commands with one parameter
        if next_byte in (0x21, 0x25, 0x33, 0x64, 0x74, 0x26, 0x2A, 0x3D, 0x3A, 0x52, 0x63, 0x70, 0x72, 0x73, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A,
                        0x61, 0x45, 0x2D, 0x4A, 0x4B, 0x56, 0x62, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6C, 0x6D, 0x6E, 0x6F, 0x70):
            return 3
        # ESC 2 sets the default line spacing and has no parameter
        if next_byte == 0x32:
            return 2
        # Common two-byte commands not requiring a parameter
        if next_byte in (0x52,):
            return 2
        return 0

    # (The remaining command families are handled below.)
    if False:
        return 0
        # ESC GS ... (0x1B 0x1D) - delegate to GS handler
        if next_byte == 0x1D and third_byte is not None:
            return _get_escpos_command_length(0x1D, third_byte) + 1
        return 0

    # GS (0x1D) commands
    if cmd_byte == 0x1D:
        if next_byte is None:
            return 0
        # GS V (cut) - 3 or 4 bytes
        if next_byte == 0x56:
            if third_byte in (0x41, 0x42, 0x61, 0x62):  # with feed amount
                return 4
            return 3
        # GS v 0 (raster) - variable, handled separately
        if next_byte == 0x76:
            return 0  # special handling
        # GS k (barcode) - variable
        if next_byte == 0x6B:
            return 0  # special handling
        # GS ( k / ( L / ( k (QR) - variable
        if next_byte == 0x28:
            return 0  # special handling
        # 3-byte GS commands
        if next_byte in (0x21, 0x42, 0x4C, 0x57, 0x68, 0x77, 0x49, 0x58, 0x59):
            return 3
        return 2  # default 2 bytes for unknown GS

    # FS (0x1C) commands
    if cmd_byte == 0x1C:
        if next_byte is None:
            return 0
        return 2

    # DLE (0x10) commands
    if cmd_byte == 0x10:
        if next_byte is None:
            return 0
        return 2

    return 0


def _parse_escpos_segments(raw_bytes):
    """
    Parse ESC/POS stream into segments: (type, data)
    type: 'text' (bytes to render as raster), 'control' (raw ESC/POS commands)
    """
    segments = []
    i = 0
    text_buffer = bytearray()

    while i < len(raw_bytes):
        b = raw_bytes[i]

        # Check for ESC/POS control sequences
        cmd_len = 0
        if b == 0x1B:
            cmd_len = _get_escpos_command_length(0x1B,
                raw_bytes[i+1] if i+1 < len(raw_bytes) else None,
                raw_bytes[i+2] if i+2 < len(raw_bytes) else None)
        elif b == 0x1D:
            cmd_len = _get_escpos_command_length(0x1D,
                raw_bytes[i+1] if i+1 < len(raw_bytes) else None,
                raw_bytes[i+2] if i+2 < len(raw_bytes) else None)
        elif b == 0x1C:
            cmd_len = _get_escpos_command_length(0x1C,
                raw_bytes[i+1] if i+1 < len(raw_bytes) else None)
        elif b == 0x10:
            cmd_len = _get_escpos_command_length(0x10,
                raw_bytes[i+1] if i+1 < len(raw_bytes) else None)
        elif b in (0x0A, 0x0D, 0x0C):  # LF, CR, FF
            cmd_len = 1

        # Special handling for variable-length commands
        if b == 0x1D and i + 2 < len(raw_bytes) and raw_bytes[i+1] == 0x76:
            # GS v 0 - raster image, skip entire image data
            if raw_bytes[i+2] == 0x30 and i + 7 < len(raw_bytes):
                w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
                h = raw_bytes[i+6] + raw_bytes[i+7] * 256
                data_len = w_bytes * h
                if w_bytes > 0 and h > 0 and i + 8 + data_len <= len(raw_bytes):
                    cmd_len = 8 + data_len

        elif b == 0x1B and i + 2 < len(raw_bytes) and raw_bytes[i+1] == 0x1D and raw_bytes[i+2] == 0x53:
            # ESC GS S - Star/Bixolon raster
            if i + 7 < len(raw_bytes):
                w_bytes = raw_bytes[i+4] + raw_bytes[i+5] * 256
                h = raw_bytes[i+6] + raw_bytes[i+7] * 256
                data_len = w_bytes * h
                if w_bytes > 0 and h > 0 and i + 8 + data_len <= len(raw_bytes):
                    cmd_len = 8 + data_len

        if cmd_len > 0:
            # Flush text buffer first
            if text_buffer:
                segments.append(('text', bytes(text_buffer)))
                text_buffer = bytearray()

            # Consume the control sequence
            segments.append(('control', raw_bytes[i:i+cmd_len]))
            i += cmd_len
        else:
            # Printable character (or part of UTF-8 sequence)
            text_buffer.append(b)
            i += 1

    # Flush remaining text
    if text_buffer:
        segments.append(('text', bytes(text_buffer)))

    return segments


# ==================== FULL ESC/POS VISUAL RENDERER ====================

class ESCPOSRenderer:
    """Render ESC/POS stream as single bitmap (visual receipt)."""

    def __init__(self, font_size=24, max_width=576, font_path=None, line_spacing=1.0):
        self.font_size = font_size
        self.max_width = max_width
        self.font_path = font_path
        self.line_spacing = line_spacing

        # Formatting state
        self.align = 'left'  # left, center, right
        self.bold = False
        self.underline = False
        self.font_height_mult = 1
        self.font_width_mult = 1
        self.upside_down = False
        self.inverted = False

        # Line buffer
        self.lines = []  # list of (text, format_dict)
        self.current_line = ""
        self.current_format = {}

        # Cut command to pass through
        self.cut_command = None

    def _get_font(self, bold=False):
        """Get font with bold variant."""
        from epp import _get_font
        font = _get_font(self.font_size, self.font_path)
        return font

    def _apply_format(self, fmt):
        """Update formatting state from format dict."""
        if 'align' in fmt:
            self.align = fmt['align']
        if 'bold' in fmt:
            self.bold = fmt['bold']
        if 'underline' in fmt:
            self.underline = fmt['underline']
        if 'font_height' in fmt:
            self.font_height_mult = fmt['font_height']
        if 'font_width' in fmt:
            self.font_width_mult = fmt['font_width']
        if 'upside_down' in fmt:
            self.upside_down = fmt['upside_down']
        if 'inverted' in fmt:
            self.inverted = fmt['inverted']

    def _flush_line(self, force=False):
        """Save the current line while keeping ESC/POS formatting state."""
        if self.current_line or force:
            self.lines.append({
                'text': self.current_line,
                'format': dict(self.current_format)
            })
            self.current_line = ""

    def add_text(self, text):
        """Add text to current line."""
        self.current_line += text

    @staticmethod
    def _clean_text(text):
        """Remove printer control bytes before measuring or drawing text."""
        return ''.join(
            ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F
        )

    @classmethod
    def _effective_format(cls, text, fmt):
        """Apply receipt-specific size rules before drawing a line."""
        effective = dict(fmt)
        clean_text = cls._clean_text(text).lstrip().upper()

        # Transaction identifiers use the same normal size as menu lines,
        # with a light bold stroke.  Some POS clients send ESC ! 0x38 here,
        # which requests double width and double height and causes clipping.
        if clean_text.startswith('#SMDC'):
            effective.update({
                'bold': True,
                'font_width': 1,
                'font_height': 1,
                'underline': False,
            })

        # KITCHEN heading should use the same normal size as menu text,
        # with bold enabled for emphasis.
        if clean_text == 'KITCHEN':
            effective.update({
                'bold': True,
                'font_width': 1,
                'font_height': 1,
                'underline': False,
            })
        return effective

    def _line_text(self, text):
        """Return printable text used consistently for measure and draw."""
        return self._clean_text(text)

    def add_control(self, ctrl_bytes):
        """Process ESC/POS control command."""
        if not ctrl_bytes:
            return

        cmd = ctrl_bytes[0]

        # LF/CR/FF - line break
        if cmd in (0x0A, 0x0D, 0x0C):
            self._flush_line()

            # Some POS clients leave the transaction header's large/bold
            # mode active.  Reset only size/weight after that line so the
            # following date and outlet information uses the normal size.
            if self.lines:
                previous_text = ''.join(
                    ch for ch in self.lines[-1]['text']
                    if ord(ch) >= 0x20 and ord(ch) != 0x7F
                ).lstrip().upper()
                if previous_text.startswith('#SMDC'):
                    current_align = self.current_format.get('align', 'left')
                    self.current_format = {'align': current_align}
                    self.bold = False
                    self.font_height_mult = 1
                    self.font_width_mult = 1
            return

        # ESC commands
        if cmd == 0x1B and len(ctrl_bytes) >= 2:
            sub = ctrl_bytes[1]

            # ESC @ - init (reset formatting only, keep lines)
            if sub == 0x40:
                self._flush_line()
                # Reset formatting state only, keep accumulated lines
                self.align = 'left'
                self.bold = False
                self.underline = False
                self.font_height_mult = 1
                self.font_width_mult = 1
                self.upside_down = False
                self.inverted = False
                self.current_format = {}
                return

            # ESC a n - alignment
            if sub == 0x61 and len(ctrl_bytes) >= 3:
                n = ctrl_bytes[2]
                if n == 0 or n == 0x30:
                    self.current_format['align'] = 'left'
                elif n == 1 or n == 0x31:
                    self.current_format['align'] = 'center'
                elif n == 2 or n == 0x32:
                    self.current_format['align'] = 'right'
                return

            # ESC ! n - print mode (bold, double height/width, underline)
            if sub == 0x21 and len(ctrl_bytes) >= 3:
                n = ctrl_bytes[2]
                self.current_format['bold'] = bool(n & 0x08)
                self.current_format['underline'] = bool(n & 0x80)
                # Double height (bit 4)
                self.current_format['font_height'] = 2 if (n & 0x10) else 1
                # Double width (bit 5)
                self.current_format['font_width'] = 2 if (n & 0x20) else 1
                return

            # ESC - n - underline
            if sub == 0x2D and len(ctrl_bytes) >= 3:
                n = ctrl_bytes[2]
                self.current_format['underline'] = (n != 0)
                return

            # ESC E n - bold
            if sub == 0x45 and len(ctrl_bytes) >= 3:
                self.current_format['bold'] = (ctrl_bytes[2] != 0)
                return

            # ESC { n - upside down
            if sub == 0x7B and len(ctrl_bytes) >= 3:
                self.current_format['upside_down'] = (ctrl_bytes[2] != 0)
                return

            # ESC t n - code page (ignore for rendering)
            if sub == 0x74:
                return

            # ESC V n - 90° rotation (ignore)
            if sub == 0x56:
                return

            # ESC d n - feed n lines
            if sub == 0x64 and len(ctrl_bytes) >= 3:
                for _ in range(ctrl_bytes[2]):
                    self._flush_line()
                return

            # ESC 2 / ESC 3 - line spacing
            if sub in (0x32, 0x33):
                return

        # GS commands
        if cmd == 0x1D and len(ctrl_bytes) >= 2:
            sub = ctrl_bytes[1]

            # GS V - cut (save for passthrough)
            if sub == 0x56:
                self.cut_command = ctrl_bytes
                return

            # GS ! n - character size using official Epson field codes
            if sub == 0x21 and len(ctrl_bytes) >= 3:
                w_mult, h_mult = decode_gs_character_size(ctrl_bytes[2])
                self.current_format['font_width'] = w_mult
                self.current_format['font_height'] = h_mult
                return

            # GS B n - inverse
            if sub == 0x42 and len(ctrl_bytes) >= 3:
                self.current_format['inverted'] = (ctrl_bytes[2] != 0)
                return

            # GS b n - smoothing (ignore)
            if sub == 0x62:
                return

            # GS k - barcode (ignore for now)
            if sub == 0x6B:
                return

            # GS ( k - QR code (ignore)
            if sub == 0x28:
                return

        # FS commands (ignore)
        if cmd == 0x1C:
            return

        # DLE commands (ignore)
        if cmd == 0x10:
            return

    def render(self):
        """Render all lines to single bitmap."""
        if not self.lines and not self.current_line:
            return None, None, None

        # Legacy mode now uses LegacyRasterRenderer; this class remains for
        # compatibility with existing hybrid tests and callers.

        # Flush any remaining line
        self._flush_line()

        from epp import _get_font, _build_raster_command
        from PIL import Image, ImageDraw

        # Calculate line height
        base_line_height = int(self.font_size * self.line_spacing)

        # First pass: calculate heights
        line_heights = []
        for line_info in self.lines:
            text = line_info['text']
            fmt = self._effective_format(text, line_info['format'])
            text = self._line_text(text)

            h_mult = fmt.get('font_height', 1)
            w_mult = fmt.get('font_width', 1)
            line_font_size = self.font_size * h_mult

            font = _get_font(line_font_size, self.font_path)
            if not text.strip():
                line_heights.append(int(base_line_height * 0.5))
                continue

            dummy = Image.new('1', (1, 1))
            draw = ImageDraw.Draw(dummy)
            bbox = draw.textbbox((0, 0), text, font=font)
            text_h = bbox[3] - bbox[1]
            line_h = max(int(text_h * 1.2), int(base_line_height * h_mult))
            line_heights.append(line_h)

        total_h = sum(line_heights)

        # Create bitmap
        w_bytes = (self.max_width + 7) // 8
        img = Image.new('1', (self.max_width, total_h), 1)
        draw = ImageDraw.Draw(img)

        # Draw each line
        y = 0
        for i, line_info in enumerate(self.lines):
            text = line_info['text']
            fmt = self._effective_format(text, line_info['format'])
            text = self._line_text(text)
            line_h = line_heights[i]

            if not text.strip():
                y += line_h
                continue

            h_mult = fmt.get('font_height', 1)
            w_mult = fmt.get('font_width', 1)
            bold = fmt.get('bold', False)
            underline = fmt.get('underline', False)
            align = fmt.get('align', 'left')
            inverted = fmt.get('inverted', False)

            # Keep the normal receipt font at the configured size.  The
            # transaction id uses this same size, with only a light bold stroke.
            base_font_size = self.font_size
            line_font_size = base_font_size * h_mult
            font = _get_font(line_font_size, self.font_path)

            dummy = Image.new('1', (1, 1))
            d = ImageDraw.Draw(dummy)

            # Text has already been cleaned before measuring and drawing.
            clean_text = text

            # Apply font_width multiplier by checking if we need to scale
            # For double-width (w_mult=2), we need to handle wider text
            effective_max_width = self.max_width // w_mult if w_mult > 1 else self.max_width

            bbox = d.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            # Check wrapping against effective width (accounting for width multiplier)
            if text_w > effective_max_width:
                # Word wrap: split into multiple lines
                words = clean_text.split(' ')
                wrapped_lines = []
                current_line = ""
                for word in words:
                    test_line = current_line + (" " if current_line else "") + word
                    test_bbox = d.textbbox((0, 0), test_line, font=font)
                    test_w = test_bbox[2] - test_bbox[0]
                    if test_w <= effective_max_width:
                        current_line = test_line
                    else:
                        if current_line:
                            wrapped_lines.append(current_line)
                        current_line = word
                if current_line:
                    wrapped_lines.append(current_line)
            else:
                wrapped_lines = [clean_text]

            # Draw each wrapped line
            for line_idx, line_text in enumerate(wrapped_lines):
                if not line_text.strip():
                    y += int(line_h / len(wrapped_lines)) if len(wrapped_lines) > 1 else line_h
                    continue

                line_bbox = d.textbbox((0, 0), line_text, font=font)
                line_text_w = line_bbox[2] - line_bbox[0]

                if align == 'center':
                    line_x = (self.max_width - line_text_w * w_mult) // 2
                elif align == 'right':
                    line_x = self.max_width - line_text_w * w_mult
                else:
                    line_x = 0

                if inverted:
                    draw.rectangle([line_x, y, line_x + line_text_w * w_mult, y + text_h], fill=0)
                    # For inverted, draw each char with width multiplier
                    if w_mult > 1:
                        for ch in line_text:
                            draw.text((line_x, y), ch, font=font, fill=1)
                            if w_mult > 1:
                                # Draw again shifted for double-width
                                char_bbox = d.textbbox((0, 0), ch, font=font)
                                char_w = char_bbox[2] - char_bbox[0]
                                draw.text((line_x + char_w, y), ch, font=font, fill=1)
                            line_x += char_w * w_mult
                    else:
                        draw.text((line_x, y), line_text, font=font, fill=1)
                else:
                    if w_mult > 1:
                        # Scale each glyph horizontally without emitting it twice.
                        # Repeating the glyph at an offset makes characters look doubled.
                        for ch in line_text:
                            char_bbox = d.textbbox((0, 0), ch, font=font)
                            char_w = max(1, char_bbox[2] - char_bbox[0])
                            if w_mult == 1:
                                draw.text((line_x, y), ch, font=font, fill=0)
                            else:
                                # Render to a temporary mask, then stretch it horizontally.
                                glyph = Image.new('1', (char_w + 4, text_h + 4), 1)
                                glyph_draw = ImageDraw.Draw(glyph)
                                glyph_draw.text((0, 0), ch, font=font, fill=0)
                                glyph = glyph.resize((glyph.width * w_mult, glyph.height), Image.Resampling.NEAREST)
                                img.paste(glyph, (line_x, y))
                            line_x += char_w * w_mult
                    else:
                        # Draw each character exactly once.  Use a one-pixel
                        # stroke for bold so the glyph stays the same size.
                        draw.text(
                            (line_x, y),
                            line_text,
                            font=font,
                            fill=0,
                            stroke_width=1 if bold else 0,
                            stroke_fill=0 if bold else None,
                        )
                    if underline:
                        uy = y + text_h + 1
                        draw.line([line_x, uy, line_x + line_text_w * w_mult, uy], fill=0, width=1)

                # Increment y for this line
                y += int(line_h / len(wrapped_lines)) if len(wrapped_lines) > 1 else line_h

        # Convert to ESC/POS raster
        bitmap = bytearray(w_bytes * total_h)
        pixels = img.load()
        for row in range(total_h):
            for col_byte in range(w_bytes):
                byte_val = 0
                for bit in range(8):
                    x = col_byte * 8 + bit
                    if x < self.max_width:
                        if pixels[x, row] == 0:
                            byte_val |= (0x80 >> bit)
                bitmap[row * w_bytes + col_byte] = byte_val

        return w_bytes, total_h, bytes(bitmap)


def convert_text_to_raster(raw_bytes, font_size=24, max_width=576, font_path=None, line_spacing=1.0):
    """
    Convert ESC/POS stream to a single bitmap (full receipt visual).
    All formatting (bold, center, align, underline) is rendered visually.
    Only the GS V cut command passes through.
    """
    segments = _parse_escpos_segments(raw_bytes)
    renderer = ESCPOSRenderer(font_size, max_width, font_path, line_spacing)

    for seg_type, data in segments:
        if seg_type == 'control':
            renderer.add_control(data)
        else:
            # POS systems commonly send GBK; retain UTF-8 as a fallback.
            text = None
            for encoding in ('gbk', 'gb2312', 'utf-8'):
                try:
                    text = data.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                text = data.decode('utf-8', errors='replace')
            renderer.add_text(text)

    render_result = renderer.render()
    if render_result is None or render_result[0] is None:
        # No printable content: preserve only the cut command.
        result_bytes = bytearray()
        for seg_type, data in segments:
            if (seg_type == 'control' and len(data) >= 2
                    and data[0] == 0x1D and data[1] == 0x56):
                result_bytes.extend(data)
        return bytes(result_bytes)

    w_bytes, height, bitmap = render_result
    result = bytearray(_build_raster_command(w_bytes, height, bitmap))
    if renderer.cut_command:
        result.extend(renderer.cut_command)
    return bytes(result)


def should_use_raster(raw_bytes):
    """Check if data needs raster rendering."""
    # Check for any byte >= 0x80 (non-ASCII in UTF-8)
    for b in raw_bytes:
        if b >= 0x80:
            return True

    # Also check for ESC/POS formatting commands that printer may not support properly
    # ESC ! n (print mode: bold, double-width/height)
    # GS ! n (character size)
    # ESC a n (alignment)
    for i, b in enumerate(raw_bytes):
        if b == 0x1B and i + 2 < len(raw_bytes):
            if raw_bytes[i+1] == 0x21:  # ESC ! n
                return True
        if b == 0x1D and i + 2 < len(raw_bytes):
            if raw_bytes[i+1] == 0x21:  # GS ! n
                return True

    return False


@app.route("/view/<int:job_id>")
def view_job(job_id):
    date_param = request.args.get("date", "").strip()
    today = datetime.now().strftime("%Y-%m-%d")
    if date_param and date_param != today:
        history = load_backup_history(date_param)
    else:
        history = load_print_history()

    for job in history:
        if job["id"] == job_id:
            source_hex = job.get("source_raw_data", job.get("raw_data", ""))
            output_hex = job.get("output_raw_data", "")
            source_bytes = bytes.fromhex(source_hex)
            output_bytes = bytes.fromhex(output_hex) if output_hex else source_bytes
            source_tokens = parse_stream(source_bytes)
            output_tokens = parse_stream(output_bytes)
            return {
                "status": "success",
                "raw_data": strip_escpos_images(source_bytes).hex(),
                "source_raw_data": source_bytes.hex(),
                "output_raw_data": output_bytes.hex(),
                "images": extract_escpos_images(output_bytes),
                "source_images": extract_escpos_images(source_bytes),
                "conversion_mode": job.get("conversion_mode", "legacy"),
                "source_size": len(source_bytes),
                "output_size": len(output_bytes),
                "source_token_count": len(source_tokens),
                "output_token_count": len(output_tokens),
            }


    return {"status": "error", "message": "Job not found"}, 404

@app.route("/history/dates", methods=["GET"])
def history_dates():
    today = datetime.now().strftime("%Y-%m-%d")
    dates = [d for d in list_available_backup_dates() if d != today]
    return jsonify({"today": today, "dates": dates})

@app.route("/history/archive/<date>", methods=["GET"])
def history_archive(date):
    today = datetime.now().strftime("%Y-%m-%d")
    if date == today:
        history = load_print_history()
    else:
        history = load_backup_history(date)
    return jsonify({"status": "success", "date": date, "history": history})

@app.route("/restart", methods=["GET"])
def restart_server():
    threading.Thread(target=restart_print_server, daemon=True).start()
    return render_template("restart.html")

def run_servers():
    global server_thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()
    threading.Thread(target=lambda: serve(app, host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

def exit_tray(icon, menu_item):
    icon.stop()

def open_dashboard(icon=None, menu_item=None):
    webbrowser.open(f"http://localhost:{FLASK_PORT}")

def is_service_running():
    """Cek apakah service EPPrintServer sedang berjalan."""
    try:
        status = win32serviceutil.QueryServiceStatus("EPPrintServer")
        return status[1] == win32service.SERVICE_RUNNING
    except Exception:
        return False

def start_service():
    """Start service EPPrintServer."""
    try:
        win32serviceutil.StartService("EPPrintServer")
        logging.info("🚀 Service EPPrintServer dimulai dari launcher.")
        return True
    except Exception as e:
        logging.error(f"❌ Gagal start service: {e}")
        return False

def is_tray_running():
    """Cek apakah tray icon sudah berjalan menggunakan named mutex."""
    mutex = win32event.CreateMutex(None, False, "EPPTrayMutex")
    already_exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if already_exists:
        win32api.CloseHandle(mutex)
    return already_exists, mutex

def launch():
    """Launcher: start service jika belum jalan, lalu buka dashboard."""
    import time
    if is_service_running():
        logging.info("✅ Service sudah berjalan, membuka dashboard...")
    else:
        logging.info("⏳ Service belum berjalan, memulai service...")
        if start_service():
            # Tunggu service ready & Flask siap menerima koneksi
            for _ in range(15):
                time.sleep(1)
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect(("localhost", FLASK_PORT))
                    s.close()
                    break
                except Exception:
                    continue
            logging.info("✅ Service berhasil dimulai.")
        else:
            logging.error("❌ Gagal memulai service.")

    # Selalu buka dashboard
    open_dashboard()

    # Jalankan tray icon hanya jika belum ada yang jalan
    already_running, mutex = is_tray_running()
    if already_running:
        logging.info("ℹ️ Tray icon sudah berjalan, tidak buat duplikat.")
    else:
        run_tray(mutex)

def run_tray(mutex=None):
    """Jalankan tray icon. mutex: jika None, buat mutex baru untuk mencegah duplikat."""
    if mutex is None:
        already_running, mutex = is_tray_running()
        if already_running:
            logging.info("ℹ️ Tray icon sudah berjalan, tidak buat duplikat.")
            return

    ICON_PATH = ensure_icon_available()
    if not os.path.exists(ICON_PATH):
        logging.error("File icon.png tidak ditemukan")
        win32api.CloseHandle(mutex)
        return
    image = Image.open(ICON_PATH)
    menu = Menu(
        item('Open Dashboard', open_dashboard),
        item('Quit', exit_tray)
    )
    pystray.Icon("EPP", image, "EPP", menu).run()
    win32api.CloseHandle(mutex)


class EPPService(win32serviceutil.ServiceFramework):
    _svc_name_ = "EPPrintServer"
    _svc_display_name_ = "EPP Print Server"
    _svc_description_ = "ESC/POS Print Server for thermal printers"
    _exe_name_ = sys.executable
    _exe_args_ = None

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        stop_event.set()
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        os.chdir(get_app_dir())
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        logging.info("🚀 EPP Service started.")
        run_servers()
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        logging.info("🛑 EPP Service stopped.")


if __name__ == "__main__":
    os.chdir(get_app_dir())

    if len(sys.argv) > 1 and sys.argv[1] == '--launch':
        # Desktop shortcut: start service jika belum jalan, buka dashboard
        launch()
    elif len(sys.argv) > 1 and sys.argv[1] == '--tray':
        # Tray only — tidak start server, hanya icon
        run_tray()
    elif getattr(sys, 'frozen', False) and len(sys.argv) == 1:
        # Started by Windows Service Manager
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(EPPService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # Development mode
        run_servers()
        run_tray()
