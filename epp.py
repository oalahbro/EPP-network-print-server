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
from PIL import Image
from flask import Flask, jsonify, render_template, request, redirect, url_for
from waitress import serve
from datetime import datetime

# Konfigurasi
CONFIG_FILE = "conf.json"
LOG_FILE = "server_log.txt"
PRINT_HISTORY_FILE = "print_history.json"
LOG_MAX_SIZE = 5 * 1024 * 1024
DEFAULT_PORT = 9100
FLASK_PORT = 5000
MAX_REPRINT = 3
HOST = "0.0.0.0"
BUFFER_SIZE = 2048
MAX_DATA_SIZE = 10 * 1024 * 1024  # 10 MB max data size

# Thread lock for shared state
_lock = threading.Lock()

MAX_ERRORS = 20  # Keep last N errors in memory

def record_error(message):
    """Record an error to the in-memory error list for /health visibility."""
    entry = {"timestamp": str(datetime.now()), "message": str(message)}
    with _lock:
        status["errors"].append(entry)
        status["errors"] = status["errors"][-MAX_ERRORS:]


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
status = {"last_request": None, "total_jobs": 0, "errors": []}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        default_config = {
            "DEFAULT": "HAKA",
            "PRINTER_NAME": r"\\LAPTOP-EN2ING59\HAKA",
            "PORT": DEFAULT_PORT,
            "FLASK_PORT": FLASK_PORT,
            "MAX_REPRINT": MAX_REPRINT
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
def load_print_history():
    if not os.path.exists(PRINT_HISTORY_FILE):
        with open(PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=4)
        return []

    with open(PRINT_HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_print_history(history):
    with open(PRINT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4)

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
        MAX_REPRINT = int(config.get("MAX_REPRINT", 0))
        history = load_print_history()

        if not PRINTER_NAME:
            raise ValueError("Printer name not found in config.")

        if job_id is not None:
            job_found = None
            for job in history:
                if job["id"] == job_id:
                    job_found = job
                    break

            if job_found is None:
                return {"status": False, "message": "Job not found"}

            current_count = job_found.get("print_count", 0)

            # Cek max reprint
            if current_count >= MAX_REPRINT:
                logging.warning("❌ Max reprint reached")
                record_error(f"Max reprint reached for job {job_id}")
                return {"status": False, "message": "Max reprint reached"}

            # Tambah counter
            current_count += 1
            job_found["print_count"] = current_count

            logging.info(f"🔁 Reprint Job ID: {job_id} (Count: {current_count})")
            logging.info(f"🖨️ Mengirim ke printer: {PRINTER_NAME}")

            # Tambahkan label REPRINT + count
            data = add_reprint_mark(data, current_count)
            with _lock:
                save_print_history(history)
        else:
            logging.info("📃 Print job baru diterima")
            logging.info(f"🖨️ Mengirim ke printer: {PRINTER_NAME}")

        hprinter = win32print.OpenPrinter(PRINTER_NAME)
        try:
            job_info = win32print.StartDocPrinter(hprinter, 1, ("Print Job EPP", None, "RAW"))
            win32print.StartPagePrinter(hprinter)
            win32print.WritePrinter(hprinter, data)
            win32print.EndPagePrinter(hprinter)
            win32print.EndDocPrinter(hprinter)
        finally:
            win32print.ClosePrinter(hprinter)

        with _lock:
            status["total_jobs"] += 1
            status["last_request"] = str(datetime.now())
        logging.info("✅ Cetak berhasil.")
        logging.info(f"Print data: {len(data)} bytes")


        if job_id is None:
            job_entry = {
                "id": len(history) + 1,
                "printer": PRINTER_NAME,
                "timestamp": str(datetime.now()),
                "size": len(data),
                "raw_data": data.hex(),
                "print_count": 0
            }

            history.insert(0, job_entry)  # job terbaru di atas
            history = history[:500]
            with _lock:
                save_print_history(history)
            logging.info(f"🧾 History tersimpan. Total job: {len(history)}")

        return {"status": True}
    except Exception as e:
        logging.error(f"❌ Kesalahan printer: {e}")
        record_error(f"Printer error: {e}")
        return {"status": False, "message": str(e)}

def check_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0

def start_server():
    logging.info("🛠 Starting print server ...")
    config = load_config()
    port = config.get("PORT", DEFAULT_PORT)
    history = load_print_history()

    if check_port_in_use(port):
        logging.error(f"❌ Port {port} sudah digunakan! Aplikasi dihentikan.")
        record_error(f"Port {port} already in use")
        os._exit(1)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind((HOST, port))
            server.listen(5)
            logging.info(f"🚀 Print server running on port {port}...")

            while True:
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
                                    if len(data) > MAX_DATA_SIZE:
                                        logging.warning(f"⚠️ Data dari {addr} melebihi batas {MAX_DATA_SIZE} bytes, memotong koneksi")
                                        record_error(f"Data from {addr} exceeded {MAX_DATA_SIZE} bytes")
                                        break
                                except socket.timeout:
                                    break  # Timeout, asumsi data selesai

                            if data and len(data) <= MAX_DATA_SIZE:
                                if data.startswith(b"\x1b@"):
                                    logging.info("📃 Deteksi ESC/POS data (kasir)")
                                else:
                                    logging.info("📄 Deteksi dokumen non-ESC/POS (umum)")

                                logging.info(f"🖨 Mengirim {len(data)} bytes ke printer...")
                                send_to_printer(data)


                        except ConnectionResetError as e:
                            logging.warning(f"⚠️ Koneksi dengan {addr} terputus secara paksa: {e}")
                            record_error(f"Connection reset from {addr}: {e}")
                        except Exception as e:
                            logging.error(f"❌ Error tidak terduga saat menerima data dari {addr}: {e}")
                            record_error(f"Unexpected error from {addr}: {e}")

                except OSError as e:
                    logging.error(f"❌ Error saat menerima koneksi: {e}")
                    record_error(f"Connection accept error: {e}")

    except OSError as e:
        logging.error(f"❌ Gagal menjalankan server: {e}")
        record_error(f"Server start failed: {e}")
        os._exit(1)


def clean_log_text(text):
    """ Membersihkan karakter escape sequence dan merapikan teks log """
    text = re.sub(r'[\x1b\x1d][@\w]*', '', text)  # Hapus karakter escape seperti \x1b, \x1d
    return text.strip()

def read_log():
    """ Membaca log dari file dan membersihkan encoding """
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
            raw_logs = f.readlines()

        cleaned_logs = []
        for line in raw_logs:
            cleaned_logs.append(clean_log_text(line))

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

        try:
            new_port_int = int(new_port)
            new_maxreprint_int = int(new_maxreprint)
        except ValueError:
            logs = read_log()
            return render_template("dashboard.html", status=status, config=config, logs=logs, printers=printers, default_printer=default_printer, history=history, error="Port dan Max Reprint harus berupa angka.")

        computer_name = os.environ.get('COMPUTERNAME', socket.gethostname())
        new_printer_path = f"\\\\{computer_name}\\{new_default}"
        config["DEFAULT"] = new_default
        config["PRINTER_NAME"] = new_printer_path
        config["PORT"] = new_port_int
        config["MAX_REPRINT"] = new_maxreprint_int
        with _lock:
            save_config(config)

        return redirect(url_for("restart_server"))

    logs = read_log()
    return render_template("dashboard.html", status=status, config=config, logs=logs, printers=printers, default_printer=default_printer, history=history)

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

@app.route("/view/<int:job_id>")
def view_job(job_id):
    history = load_print_history()

    for job in history:
        if job["id"] == job_id:
            return {
                "status": "success",
                "raw_data": job["raw_data"]
            }

    return {"status": "error", "message": "Job not found"}, 404

@app.route("/health")
def health():
    with _lock:
        current_status = dict(status)
    config = load_config()
    errors = current_status.get("errors", [])
    return jsonify({
        "status": "ok" if not errors else "degraded",
        "total_jobs": current_status["total_jobs"],
        "last_request": current_status["last_request"],
        "printer": config.get("PRINTER_NAME", ""),
        "port": config.get("PORT", DEFAULT_PORT),
        "error_count": len(errors),
        "recent_errors": errors[-5:]
    })

@app.route("/history/delete/<int:job_id>", methods=["POST"])
def delete_job(job_id):
    with _lock:
        history = load_print_history()
        history = [job for job in history if job["id"] != job_id]
        save_print_history(history)
    return {"status": "success", "message": "Job deleted"}

@app.route("/history/clear", methods=["POST"])
def clear_history():
    with _lock:
        save_print_history([])
    return {"status": "success", "message": "History cleared"}

@app.route("/restart", methods=["GET"])
def restart_server():
    logging.info("🔄 Aplikasi akan restart untuk menerapkan perubahan port.")

    def restart_app():
        python = sys.executable
        os.execl(python, python, *sys.argv)

    threading.Thread(target=restart_app, daemon=True).start()
    return render_template("restart.html")

def run_servers():
    threading.Thread(target=start_server, daemon=True).start()
    threading.Thread(target=lambda: serve(app, host="0.0.0.0", port=FLASK_PORT), daemon=True).start()

def exit_app(icon, item):
    icon.stop()
    os._exit(0)

def run_tray():
    ICON_PATH = ensure_icon_available()
    if not os.path.exists(ICON_PATH):
        logging.error("File icon.png tidak ditemukan")
        return
    image = Image.open(ICON_PATH)
    menu = Menu(item('Quit', exit_app))
    pystray.Icon("EPP", image, "EPP", menu).run()

if __name__ == "__main__":
    run_servers()
    run_tray()
