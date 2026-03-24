import os
import sys
import socket
import win32print
import json
import sqlite3
import logging
from logging.handlers import RotatingFileHandler
import threading
import time
import pystray
import shutil
import re
from pystray import MenuItem as item, Menu
from PIL import Image
from flask import Flask, jsonify, render_template, request, redirect, url_for
from waitress import serve
from datetime import datetime

# Konfigurasi
DB_FILE = "epp_server.db"
DEFAULT_PORT = 9100
FLASK_PORT = 5000
MAX_REPRINT = 3
HOST = "0.0.0.0"
BUFFER_SIZE = 2048
MAX_LOG_ROWS = 5000
MAX_HISTORY_ROWS = 500

# Legacy files (untuk migrasi)
LEGACY_CONFIG_FILE = "conf.json"
LEGACY_HISTORY_FILE = "print_history.json"
LEGACY_LOG_FILE = "server_log.txt"

# Thread management
server_threads = {}
db_lock = threading.Lock()
threads_lock = threading.Lock()


# ==================== DATABASE ====================

def get_db():
    """Buat koneksi SQLite baru (per-call, thread-safe)."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    """Inisialisasi tabel database."""
    with db_lock:
        conn = get_db()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS printers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    printer_name TEXT NOT NULL,
                    port INTEGER NOT NULL UNIQUE,
                    max_reprint INTEGER NOT NULL DEFAULT 3
                );

                CREATE TABLE IF NOT EXISTS print_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    raw_data TEXT NOT NULL,
                    print_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
            """)

            # Set default config jika kosong
            cursor = conn.execute("SELECT COUNT(*) FROM config")
            if cursor.fetchone()[0] == 0:
                conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", ("FLASK_PORT", str(FLASK_PORT)))

            conn.commit()
        finally:
            conn.close()


def migrate_from_json():
    """Migrasi data dari file JSON/log lama ke SQLite."""
    migrated = False

    # Migrasi conf.json
    if os.path.exists(LEGACY_CONFIG_FILE):
        try:
            with open(LEGACY_CONFIG_FILE, "r") as f:
                old_config = json.load(f)

            with db_lock:
                conn = get_db()
                try:
                    # Cek apakah sudah ada printers
                    cursor = conn.execute("SELECT COUNT(*) FROM printers")
                    if cursor.fetchone()[0] == 0:
                        if "PRINTERS" in old_config:
                            # Format multi-printer baru
                            flask_port = old_config.get("FLASK_PORT", FLASK_PORT)
                            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("FLASK_PORT", str(flask_port)))
                            for p in old_config["PRINTERS"]:
                                conn.execute(
                                    "INSERT OR IGNORE INTO printers (id, name, printer_name, port, max_reprint) VALUES (?, ?, ?, ?, ?)",
                                    (p["id"], p["name"], p["printer_name"], p["port"], p.get("max_reprint", MAX_REPRINT))
                                )
                        else:
                            # Format lama single-printer
                            flask_port = old_config.get("FLASK_PORT", FLASK_PORT)
                            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("FLASK_PORT", str(flask_port)))
                            conn.execute(
                                "INSERT OR IGNORE INTO printers (id, name, printer_name, port, max_reprint) VALUES (?, ?, ?, ?, ?)",
                                (
                                    "printer_1",
                                    old_config.get("DEFAULT", "Default"),
                                    old_config.get("PRINTER_NAME", ""),
                                    old_config.get("PORT", DEFAULT_PORT),
                                    old_config.get("MAX_REPRINT", MAX_REPRINT)
                                )
                            )
                    conn.commit()
                finally:
                    conn.close()

            os.rename(LEGACY_CONFIG_FILE, LEGACY_CONFIG_FILE + ".bak")
            migrated = True
            logging.info("Migrasi conf.json -> SQLite selesai")
        except Exception as e:
            logging.error(f"Gagal migrasi conf.json: {e}")

    # Migrasi print_history.json
    if os.path.exists(LEGACY_HISTORY_FILE):
        try:
            with open(LEGACY_HISTORY_FILE, "r", encoding="utf-8") as f:
                old_history = json.load(f)

            if old_history:
                with db_lock:
                    conn = get_db()
                    try:
                        cursor = conn.execute("SELECT COUNT(*) FROM print_history")
                        if cursor.fetchone()[0] == 0:
                            for job in old_history:
                                conn.execute(
                                    "INSERT INTO print_history (printer, timestamp, size, raw_data, print_count) VALUES (?, ?, ?, ?, ?)",
                                    (job.get("printer", ""), job.get("timestamp", ""), job.get("size", 0), job.get("raw_data", ""), job.get("print_count", 0))
                                )
                        conn.commit()
                    finally:
                        conn.close()

            os.rename(LEGACY_HISTORY_FILE, LEGACY_HISTORY_FILE + ".bak")
            migrated = True
            logging.info("Migrasi print_history.json -> SQLite selesai")
        except Exception as e:
            logging.error(f"Gagal migrasi print_history.json: {e}")

    # Migrasi server_log.txt
    if os.path.exists(LEGACY_LOG_FILE):
        try:
            with open(LEGACY_LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            if lines:
                with db_lock:
                    conn = get_db()
                    try:
                        cursor = conn.execute("SELECT COUNT(*) FROM logs")
                        if cursor.fetchone()[0] == 0:
                            for line in lines[-MAX_LOG_ROWS:]:
                                line = line.strip()
                                if not line:
                                    continue
                                # Parse format: "2025-01-01 12:00:00,000 - INFO - message"
                                parts = line.split(" - ", 2)
                                if len(parts) >= 3:
                                    ts, level, msg = parts[0], parts[1], parts[2]
                                else:
                                    ts, level, msg = str(datetime.now()), "INFO", line
                                conn.execute(
                                    "INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)",
                                    (ts, level, msg)
                                )
                        conn.commit()
                    finally:
                        conn.close()

            # Rename log files (termasuk backup rotasi)
            for f in [LEGACY_LOG_FILE] + [f"{LEGACY_LOG_FILE}.{i}" for i in range(1, 6)]:
                if os.path.exists(f):
                    os.rename(f, f + ".bak")
            migrated = True
            logging.info("Migrasi server_log.txt -> SQLite selesai")
        except Exception as e:
            logging.error(f"Gagal migrasi server_log.txt: {e}")

    if migrated:
        logging.info("Migrasi dari file lama ke SQLite selesai. File lama di-rename ke .bak")


# ==================== SQLITE LOG HANDLER ====================

class SQLiteLogHandler(logging.Handler):
    """Custom logging handler yang menulis ke SQLite."""
    def emit(self, record):
        try:
            msg = self.format(record)
            # Bersihkan binary/escape chars dari message
            msg = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', '', msg)
            ts = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            with db_lock:
                conn = get_db()
                try:
                    conn.execute(
                        "INSERT INTO logs (timestamp, level, message) VALUES (?, ?, ?)",
                        (ts, record.levelname, msg)
                    )
                    # Trim logs jika melebihi batas
                    conn.execute("""
                        DELETE FROM logs WHERE id NOT IN (
                            SELECT id FROM logs ORDER BY id DESC LIMIT ?
                        )
                    """, (MAX_LOG_ROWS,))
                    conn.commit()
                finally:
                    conn.close()
        except Exception:
            self.handleError(record)


# ==================== CONFIG FUNCTIONS ====================

def get_config(key, default=None):
    """Ambil satu nilai config."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default
        finally:
            conn.close()

def set_config(key, value):
    """Set satu nilai config."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
        finally:
            conn.close()


# ==================== PRINTER CONFIG FUNCTIONS ====================

def get_all_printers_config():
    """Ambil semua printer dari database."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM printers ORDER BY id")
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

def get_printer_config(printer_id):
    """Ambil satu printer config by ID."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM printers WHERE id = ?", (printer_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def get_printer_config_by_name(printer_name):
    """Ambil printer config by printer_name."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM printers WHERE printer_name = ?", (printer_name,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def get_printer_config_by_port(port):
    """Ambil printer config by port."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM printers WHERE port = ?", (port,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def add_printer_config(printer_id, name, printer_name, port, max_reprint):
    """Tambah printer baru ke database."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO printers (id, name, printer_name, port, max_reprint) VALUES (?, ?, ?, ?, ?)",
                (printer_id, name, printer_name, port, max_reprint)
            )
            conn.commit()
        finally:
            conn.close()

def update_printer_config(printer_id, name, printer_name, port, max_reprint):
    """Update printer config."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute(
                "UPDATE printers SET name=?, printer_name=?, port=?, max_reprint=? WHERE id=?",
                (name, printer_name, port, max_reprint, printer_id)
            )
            conn.commit()
        finally:
            conn.close()

def delete_printer_config(printer_id):
    """Hapus printer dari database."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute("DELETE FROM printers WHERE id = ?", (printer_id,))
            conn.commit()
        finally:
            conn.close()


# ==================== PRINT HISTORY FUNCTIONS ====================

def get_print_history(limit=500):
    """Ambil print history, terbaru di atas."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM print_history ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

def get_print_job(job_id):
    """Ambil satu print job by ID."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM print_history WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

def add_print_job(printer, size, raw_data):
    """Tambah print job baru, return ID."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO print_history (printer, timestamp, size, raw_data, print_count) VALUES (?, ?, ?, ?, 0)",
                (printer, str(datetime.now()), size, raw_data)
            )
            job_id = cursor.lastrowid

            # Trim history jika melebihi batas
            conn.execute("""
                DELETE FROM print_history WHERE id NOT IN (
                    SELECT id FROM print_history ORDER BY id DESC LIMIT ?
                )
            """, (MAX_HISTORY_ROWS,))

            conn.commit()
            return job_id
        finally:
            conn.close()

def update_print_count(job_id, new_count):
    """Update print_count untuk job tertentu."""
    with db_lock:
        conn = get_db()
        try:
            conn.execute("UPDATE print_history SET print_count = ? WHERE id = ?", (new_count, job_id))
            conn.commit()
        finally:
            conn.close()


# ==================== LOG FUNCTIONS ====================

def read_log(limit=30):
    """Baca log terakhir dari database."""
    with db_lock:
        conn = get_db()
        try:
            cursor = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()  # Urutan kronologis
            return rows
        finally:
            conn.close()


# ==================== UTILITIES ====================

def get_resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS if hasattr(sys, '_MEIPASS') else os.path.dirname(sys.executable)
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def ensure_icon_available():
    ICON_PATH = get_resource_path("static/icon.png")
    temp_dir = os.path.join(os.getenv("APPDATA"), "PrintServer")
    os.makedirs(temp_dir, exist_ok=True)
    temp_icon_path = os.path.join(temp_dir, "icon.png")
    if not os.path.exists(temp_icon_path) or not file_is_same(ICON_PATH, temp_icon_path):
        shutil.copy(ICON_PATH, temp_icon_path)
    return temp_icon_path

def file_is_same(src, dst):
    return os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst)

def add_reprint_mark(data, count):
    return (
        b"\x1b\x61\x01"
        + b"\x1d\x21\x11"
        + f"*** REPRINT ({count}) ***\n".encode()
        + b"\x1d\x21\x00"
        + b"\x1b\x61\x00\n"
        + data
    )

def clean_log_text(text):
    text = re.sub(r'[\x1b\x1d][@\w]*', '', text)
    text = text.replace("\n", "<br>").strip()
    return text

def get_system_printer_list():
    printers = []
    for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_CONNECTIONS + win32print.PRINTER_ENUM_LOCAL):
        printers.append(printer[2])
    return printers

def check_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ==================== LOGGING SETUP ====================

# Setup logger (SQLite handler akan ditambah setelah init_db)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logging.getLogger("PIL").setLevel(logging.WARNING)


# ==================== FLASK APP ====================

app = Flask(__name__)
status = {"last_request": None, "total_jobs": 0}


# ==================== PRINT LOGIC ====================

def send_to_printer(data, job_id=None, printer_name=None):
    try:
        if job_id is not None:
            job = get_print_job(job_id)
            if not job:
                return {"status": False, "message": "Job not found"}

            # Gunakan printer dari history untuk reprint
            printer_name = job.get("printer", printer_name)
            printer_cfg = get_printer_config_by_name(printer_name)
            max_reprint = printer_cfg["max_reprint"] if printer_cfg else MAX_REPRINT

            current_count = job.get("print_count", 0)

            if current_count >= max_reprint:
                logging.warning("Max reprint reached")
                return {"status": False, "message": "Max reprint reached"}

            current_count += 1
            update_print_count(job_id, current_count)

            logging.info(f"Reprint Job ID: {job_id} (Count: {current_count})")
            logging.info(f"Mengirim ke printer: {printer_name}")

            data = add_reprint_mark(data, current_count)
        else:
            logging.info("Print job baru diterima")
            logging.info(f"Mengirim ke printer: {printer_name}")

        if not printer_name:
            raise ValueError("Printer name not specified.")

        hprinter = win32print.OpenPrinter(printer_name)
        win32print.StartDocPrinter(hprinter, 1, ("Print Job EPP", None, "RAW"))
        win32print.StartPagePrinter(hprinter)
        win32print.WritePrinter(hprinter, data)
        win32print.EndPagePrinter(hprinter)
        win32print.EndDocPrinter(hprinter)
        win32print.ClosePrinter(hprinter)

        status["total_jobs"] += 1
        status["last_request"] = str(datetime.now())
        logging.info("Cetak berhasil.")

        if job_id is None:
            new_id = add_print_job(printer_name, len(data), data.hex())
            logging.info(f"History tersimpan. Job ID: {new_id}")

        return {"status": True}
    except Exception as e:
        logging.error(f"Kesalahan printer: {e}")
        return {"status": False, "message": str(e)}


# ==================== SOCKET SERVERS ====================

def start_printer_server(printer_config, stop_event):
    """Socket server untuk satu printer pada satu port."""
    port = printer_config["port"]
    printer_name = printer_config["printer_name"]
    name = printer_config.get("name", printer_name)

    logging.info(f"Starting print server for '{name}' on port {port}...")

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((HOST, port))
            server.listen(5)
            server.settimeout(1.0)
            logging.info(f"Print server '{name}' running on port {port}")

            while not stop_event.is_set():
                try:
                    client, addr = server.accept()
                    logging.info(f"[{name}:{port}] Connection from {addr}")

                    with client:
                        try:
                            client.settimeout(2)
                            data = b""

                            while True:
                                try:
                                    chunk = client.recv(BUFFER_SIZE)
                                    if not chunk:
                                        break
                                    data += chunk
                                except socket.timeout:
                                    break

                            if data:
                                if data.startswith(b"\x1b@"):
                                    logging.info(f"[{name}:{port}] Deteksi ESC/POS data")
                                else:
                                    logging.info(f"[{name}:{port}] Deteksi dokumen non-ESC/POS")

                                logging.info(f"[{name}:{port}] Mengirim {len(data)} bytes ke printer...")
                                send_to_printer(data, printer_name=printer_name)

                        except ConnectionResetError as e:
                            logging.warning(f"[{name}:{port}] Koneksi terputus: {e}")
                        except Exception as e:
                            logging.error(f"[{name}:{port}] Error menerima data: {e}")

                except socket.timeout:
                    continue
                except OSError as e:
                    if not stop_event.is_set():
                        logging.error(f"[{name}:{port}] Error koneksi: {e}")

        logging.info(f"Print server '{name}' on port {port} stopped.")

    except OSError as e:
        logging.error(f"Gagal menjalankan server '{name}' pada port {port}: {e}")


def start_single_server(printer_config):
    port = printer_config["port"]

    with threads_lock:
        if port in server_threads:
            return {"status": False, "message": f"Port {port} sudah digunakan oleh printer lain"}

    if check_port_in_use(port):
        return {"status": False, "message": f"Port {port} sudah digunakan oleh proses lain"}

    stop_event = threading.Event()
    thread = threading.Thread(target=start_printer_server, args=(printer_config, stop_event), daemon=True)
    thread.start()

    with threads_lock:
        server_threads[port] = {
            "thread": thread,
            "stop_event": stop_event,
            "printer_config": printer_config
        }

    return {"status": True}


def stop_single_server(port):
    with threads_lock:
        entry = server_threads.pop(port, None)

    if not entry:
        return {"status": False, "message": f"No server running on port {port}"}

    entry["stop_event"].set()
    entry["thread"].join(timeout=5)
    logging.info(f"Server on port {port} stopped")
    return {"status": True}


def start_all_servers():
    for printer_config in get_all_printers_config():
        result = start_single_server(printer_config)
        if not result["status"]:
            logging.error(f"Failed to start server for {printer_config.get('name')}: {result['message']}")


# ==================== ROUTES ====================

@app.route("/")
def dashboard():
    history = get_print_history()
    log_rows = read_log()
    logs = []
    for row in log_rows:
        text = f"{row['timestamp']} - {row['level']} - {row['message']}"
        logs.append(clean_log_text(text))
    return render_template("dashboard.html", status=status, logs=logs, history=history)


@app.route("/api/printers", methods=["GET"])
def api_get_printers():
    printers = get_all_printers_config()
    printers_info = []
    for p in printers:
        with threads_lock:
            running = p["port"] in server_threads
        printers_info.append({**p, "running": running})
    return jsonify({"printers": printers_info, "available": get_system_printer_list()})


@app.route("/api/printers", methods=["POST"])
def api_add_printer():
    data = request.json

    name = data.get("name", "").strip()
    printer_name = data.get("printer_name", "").strip()
    port = data.get("port")
    max_reprint = data.get("max_reprint", MAX_REPRINT)

    if not name or not printer_name or not port:
        return jsonify({"status": "error", "message": "Semua field harus diisi"}), 400

    try:
        port = int(port)
        max_reprint = int(max_reprint)
    except ValueError:
        return jsonify({"status": "error", "message": "Port dan max reprint harus angka"}), 400

    # Cek duplikat port
    existing = get_printer_config_by_port(port)
    if existing:
        return jsonify({"status": "error", "message": f"Port {port} sudah digunakan oleh printer '{existing['name']}'"}), 400

    printer_id = f"printer_{int(time.time())}"

    if not printer_name.startswith("\\\\"):
        computer_name = os.environ.get('COMPUTERNAME', 'localhost')
        printer_name = f"\\\\{computer_name}\\{printer_name}"

    add_printer_config(printer_id, name, printer_name, port, max_reprint)

    new_printer = {"id": printer_id, "name": name, "printer_name": printer_name, "port": port, "max_reprint": max_reprint}

    result = start_single_server(new_printer)
    if not result["status"]:
        return jsonify({"status": "error", "message": result["message"]}), 500

    return jsonify({"status": "success", "printer": {**new_printer, "running": True}})


@app.route("/api/printers/<printer_id>", methods=["PUT"])
def api_update_printer(printer_id):
    data = request.json
    target = get_printer_config(printer_id)

    if not target:
        return jsonify({"status": "error", "message": "Printer not found"}), 404

    new_name = data.get("name", target["name"]).strip()
    new_printer_name = data.get("printer_name", target["printer_name"]).strip()
    new_port = data.get("port", target["port"])
    new_max_reprint = data.get("max_reprint", target["max_reprint"])

    try:
        new_port = int(new_port)
        new_max_reprint = int(new_max_reprint)
    except ValueError:
        return jsonify({"status": "error", "message": "Port dan max reprint harus angka"}), 400

    # Cek duplikat port (bukan diri sendiri)
    existing = get_printer_config_by_port(new_port)
    if existing and existing["id"] != printer_id:
        return jsonify({"status": "error", "message": f"Port {new_port} sudah digunakan oleh printer '{existing['name']}'"}), 400

    if not new_printer_name.startswith("\\\\"):
        computer_name = os.environ.get('COMPUTERNAME', 'localhost')
        new_printer_name = f"\\\\{computer_name}\\{new_printer_name}"

    old_port = target["port"]
    port_changed = old_port != new_port

    update_printer_config(printer_id, new_name, new_printer_name, new_port, new_max_reprint)

    updated = {"id": printer_id, "name": new_name, "printer_name": new_printer_name, "port": new_port, "max_reprint": new_max_reprint}

    # Restart server jika port atau printer berubah
    if port_changed or target["printer_name"] != new_printer_name:
        stop_single_server(old_port)
        result = start_single_server(updated)
        if not result["status"]:
            return jsonify({"status": "error", "message": result["message"]}), 500
    else:
        with threads_lock:
            if old_port in server_threads:
                server_threads[old_port]["printer_config"] = updated

    return jsonify({"status": "success", "printer": {**updated, "running": True}})


@app.route("/api/printers/<printer_id>", methods=["DELETE"])
def api_delete_printer(printer_id):
    target = get_printer_config(printer_id)

    if not target:
        return jsonify({"status": "error", "message": "Printer not found"}), 404

    stop_single_server(target["port"])
    delete_printer_config(printer_id)

    return jsonify({"status": "success", "message": f"Printer '{target['name']}' dihapus"})


@app.route("/api/system-printers", methods=["GET"])
def api_system_printers():
    return jsonify({"printers": get_system_printer_list()})


@app.route("/reprint/<int:job_id>", methods=["POST"])
def reprint(job_id):
    job = get_print_job(job_id)
    if not job:
        return {"status": "error", "message": "Job not found"}, 404

    raw_bytes = bytes.fromhex(job["raw_data"])
    result = send_to_printer(raw_bytes, job_id)

    if result["status"]:
        return {"status": "success", "message": "Reprint berhasil"}
    else:
        return {"status": "error", "message": result.get("message", "Reprint gagal")}, 400


@app.route("/view/<int:job_id>")
def view_job(job_id):
    job = get_print_job(job_id)
    if not job:
        return {"status": "error", "message": "Job not found"}, 404
    return {"status": "success", "raw_data": job["raw_data"]}


@app.route("/restart", methods=["GET"])
def restart_server():
    return render_template("restart.html")


# ==================== STARTUP ====================

def run_servers():
    start_all_servers()
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
    # Init database & migrasi
    init_db()

    # Setup SQLite log handler (setelah DB siap)
    sqlite_handler = SQLiteLogHandler()
    sqlite_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sqlite_handler)

    # Migrasi file lama jika ada
    migrate_from_json()

    # Tambah default printer jika DB kosong
    if not get_all_printers_config():
        add_printer_config("printer_1", "Default", "", DEFAULT_PORT, MAX_REPRINT)
        logging.info("Default printer config created")

    run_servers()
    run_tray()
