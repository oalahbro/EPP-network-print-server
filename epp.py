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
from PIL import Image
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
status = {"last_request": None, "total_jobs": 0}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "DEFAULT": "HAKA",
            "PRINTER_NAME": r"\\LAPTOP-EN2ING59\HAKA",
            "PORT": DEFAULT_PORT,
            "FLASK_PORT": FLASK_PORT,
            "MAX_REPRINT": MAX_REPRINT,
            "CUT_MODE": "default"
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default_config, f, indent=4)

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    return config

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

#queue
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
    ensure_history_dir()

    if not os.path.exists(PRINT_HISTORY_FILE):
        return

    try:
        with open(PRINT_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"❌ Gagal baca print_history.json saat rotasi: {e}")
        return

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
        existing = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logging.error(f"❌ Arsip {path} rusak, akan ditimpa: {e}")
                existing = []
        merged = entries + existing
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=4)
        logging.info(f"📦 History {date_str} diarsipkan ({len(entries)} entry baru, total {len(merged)})")

    with open(PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(today_entries, f, indent=4)
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
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"❌ Gagal baca arsip {date_str}: {e}")
        return []

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
    today = datetime.now().strftime("%Y-%m-%d")
    if _last_rotation_check_date != today:
        try:
            rotate_history_if_needed()
            cleanup_old_backups()
        except Exception as e:
            logging.error(f"❌ Rotate/cleanup history gagal: {e}")
        _last_rotation_check_date = today

    if not os.path.exists(PRINT_HISTORY_FILE):
        with open(PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=4)
        return []

    with open(PRINT_HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_print_history(history):
    with open(PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4)

def modify_cut_command(data, cut_mode):
    """Modify or remove ESC/POS cut commands based on cut_mode setting.
    cut_mode: 'full', 'partial', 'none'
    """
    result = bytearray()
    i = 0
    while i < len(data):
        # GS V n (1D 56 xx) - 3 byte variant
        if (i + 2 < len(data) and data[i] == 0x1D and data[i+1] == 0x56
                and data[i+2] in (0x00, 0x01, 0x30, 0x31)):
            if cut_mode == "none":
                logging.info("✂️ Cut command removed (GS V n)")
                i += 3
            else:
                mode_byte = 0x00 if cut_mode == "full" else 0x01
                logging.info(f"✂️ Cut command replaced -> {'full' if cut_mode == 'full' else 'partial'} (GS V {mode_byte})")
                result.extend([0x1D, 0x56, mode_byte])
                i += 3
            continue

        # GS V n d (1D 56 xx xx) - 4 byte variant with feed
        if (i + 3 < len(data) and data[i] == 0x1D and data[i+1] == 0x56
                and data[i+2] in (0x41, 0x42, 0x61, 0x62)):
            feed = data[i+3]
            if cut_mode == "none":
                logging.info("✂️ Cut command removed (GS V n d)")
                i += 4
            else:
                mode_byte = 0x41 if cut_mode == "full" else 0x42
                logging.info(f"✂️ Cut command replaced -> {'full' if cut_mode == 'full' else 'partial'} (GS V {mode_byte:02X} {feed})")
                result.extend([0x1D, 0x56, mode_byte, feed])
                i += 4
            continue

        result.append(data[i])
        i += 1
    return bytes(result)


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
        config = load_config()
        PRINTER_NAME = config.get("PRINTER_NAME", "")
        MAX_REPRINT = config.get("MAX_REPRINT", "0")
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
            
            current_count = job.get("print_count", 0)

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
        
        cut_mode = config.get("CUT_MODE", "full")
        if cut_mode != "default":
            data = modify_cut_command(data, cut_mode)

        hprinter = win32print.OpenPrinter(PRINTER_NAME)
        job_info = win32print.StartDocPrinter(hprinter, 1, ("Print Job EPP", None, "RAW"))
        win32print.StartPagePrinter(hprinter)
        win32print.WritePrinter(hprinter, data)
        win32print.EndPagePrinter(hprinter)
        win32print.EndDocPrinter(hprinter)
        win32print.ClosePrinter(hprinter)

        status["total_jobs"] += 1
        status["last_request"] = str(datetime.now())
        logging.info("✅ Cetak berhasil.")
        logging.info(data)
        

        if job_id is None:
            job_entry = {
                "id": len(history) + 1,
                "printer": PRINTER_NAME,
                "timestamp": str(datetime.now()),
                "size": len(data),
                "raw_data": data.hex(),
                "print_count" : 0  # simpan dalam hex supaya aman di JSON
            }
            
            history.insert(0, job_entry)  # job terbaru di atas
            history = history[:500]
            save_print_history(history)
            logging.info(f"🧾 History tersimpan. Total job: {len(history)}")
    
        return {"status": True}
    except Exception as e:
        logging.error(f"❌ Kesalahan printer: {e}")
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
    for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_CONNECTIONS + win32print.PRINTER_ENUM_LOCAL):
        printers.append(printer[2])
    return printers

@app.route("/", methods=["GET", "POST"])
def dashboard():
    config = load_config()
    printers = get_printer_list()
    default_printer = config.get("DEFAULT", "")
    history = load_print_history()
    
    if request.method == "POST":
        new_default = request.form["default_printer"].strip()
        new_port = request.form["port"].strip()
        new_maxreprint = request.form["max_reprint"].strip()
        new_cut_mode = request.form["cut_mode"].strip()
        computer_name = os.environ['COMPUTERNAME']
        new_printer_path = f"\\\\{computer_name}\\{new_default}"
        config["DEFAULT"] = new_default
        config["PRINTER_NAME"] = new_printer_path
        config["PORT"] = int(new_port)
        config["MAX_REPRINT"] = int(new_maxreprint)
        config["CUT_MODE"] = new_cut_mode
        save_config(config)

        return redirect(url_for("restart_server"))

    logs = read_log()
    backup_dates = list_available_backup_dates()
    today_str = datetime.now().strftime("%Y-%m-%d")
    return render_template("dashboard.html", status=status, config=config, logs=logs, printers=printers, default_printer=default_printer, history=history, backup_dates=backup_dates, today_str=today_str)

@app.route("/reprint/<int:job_id>", methods=["POST"])
def reprint(job_id):
    history = load_print_history()
    
    for job in history:
        if job["id"] == job_id:
            raw_bytes = bytes.fromhex(job["raw_data"])
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
            raw_bytes = bytes.fromhex(job["raw_data"])
            images = extract_escpos_images(raw_bytes)
            text_only = strip_escpos_images(raw_bytes)
            return {
                "status": "success",
                "raw_data": text_only.hex(),
                "images": images
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
