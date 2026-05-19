"""Generate 7 hari dummy history files di C:/EPP/history.

Jalankan: python38 scripts/gen_dummy_history.py

Akan buat file print_history_YYYY-MM-DD.json untuk 7 hari terakhir (tidak termasuk hari ini).
Tiap file berisi 2-5 entry dummy dengan raw ESC/POS yang bisa dirender di modal view.
"""
import json
import os
import random
from datetime import datetime, timedelta

EPP_DIR = r"C:\EPP"
HISTORY_DIR = os.path.join(EPP_DIR, "history")
ACTIVE_FILE = os.path.join(EPP_DIR, "print_history.json")
PRINTERS = [
    r"\\LAPTOP-EN2ING59\HAKA",
    r"\\ESB-LP225\DRIVER SERVER",
    r"\\KASIR-01\EPSON-TM",
]


def build_receipt(title: str, items, total: float) -> bytes:
    """Bangun bytes ESC/POS simple: init + center title + items + total + cut."""
    out = bytearray()
    out += b"\x1b@"  # ESC @ init
    out += b"\x1b\x61\x01"  # center
    out += b"\x1d\x21\x11"  # double size bold
    out += (title + "\n").encode("ascii", errors="replace")
    out += b"\x1d\x21\x00"  # reset size
    out += b"\x1b\x61\x00"  # left
    out += b"--------------------------------\n"
    for name, price in items:
        line = f"{name:<24}{price:>8.2f}\n"
        out += line.encode("ascii", errors="replace")
    out += b"--------------------------------\n"
    out += b"\x1b\x61\x02"  # right
    out += f"TOTAL: {total:.2f}\n".encode()
    out += b"\x1b\x61\x01"  # center
    out += b"\nTerima kasih\n\n\n"
    out += b"\x1d\x56\x00"  # full cut
    return bytes(out)


def make_entry(entry_id: int, date: datetime, printer: str) -> dict:
    items = [
        ("Nasi Goreng", 25000),
        ("Es Teh Manis", 5000),
        ("Ayam Bakar", 35000),
        ("Kopi Hitam", 8000),
        ("Mie Ayam", 18000),
        ("Jus Jeruk", 12000),
    ]
    n = random.randint(2, 4)
    picked = random.sample(items, n)
    total = sum(p for _, p in picked)
    data = build_receipt(
        f"STRUK #{entry_id:03d}",
        picked,
        total,
    )
    return {
        "id": entry_id,
        "printer": printer,
        "timestamp": date.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "size": len(data),
        "raw_data": data.hex(),
        "print_count": random.randint(0, 2),
    }


def generate_day(day, now_cap=None):
    """Generate entries untuk satu hari. now_cap = datetime.now() kalau day == today."""
    count = random.randint(3, 6)
    entries = []
    for i in range(count, 0, -1):
        if now_cap is not None:
            # Jam untuk hari ini dibatasi max = jam sekarang
            max_hour = now_cap.hour
            hour = random.randint(0, max_hour) if max_hour > 0 else 0
        else:
            hour = random.randint(8, 21)
        minute = random.randint(0, 59)
        second = random.randint(0, 59)
        ts = datetime.combine(day, datetime.min.time()).replace(
            hour=hour, minute=minute, second=second, microsecond=random.randint(0, 999999)
        )
        printer = random.choice(PRINTERS)
        entries.append(make_entry(i, ts, printer))

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    for idx, e in enumerate(entries):
        e["id"] = len(entries) - idx
    return entries


def main():
    os.makedirs(HISTORY_DIR, exist_ok=True)
    now = datetime.now()
    today = now.date()
    random.seed(42)

    for days_ago in range(1, 8):  # 1..7 hari lalu
        day = today - timedelta(days=days_ago)
        entries = generate_day(day)
        fname = f"print_history_{day.strftime('%Y-%m-%d')}.json"
        path = os.path.join(HISTORY_DIR, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=4)
        print(f"Created {fname} with {len(entries)} entries")

    # Hari ini → active file (backup dulu kalau sudah ada isinya)
    if os.path.exists(ACTIVE_FILE):
        try:
            with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []
        if existing:
            backup_path = ACTIVE_FILE + ".bak"
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=4)
            print(f"Backup {len(existing)} entry lama ke print_history.json.bak")

    today_entries = generate_day(today, now_cap=now)
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(today_entries, f, indent=4)
    print(f"Created print_history.json (today) with {len(today_entries)} entries")


if __name__ == "__main__":
    main()
