# EPP Network Print Server

EPP Network Print Server adalah server cetak berbasis Python untuk Windows. Aplikasi menerima data melalui socket, lalu mengirimkannya ke printer Windows menggunakan `win32print` dengan format RAW.

Aplikasi ini ditujukan untuk printer thermal/POS yang menggunakan ESC/POS, termasuk printer yang terpasang melalui driver **POS80 Universal**, **Text/Generic Printer**, atau queue printer Windows lainnya.

## Fitur

- Menerima data cetak melalui socket TCP.
- Mengirim payload RAW ke printer Windows melalui `win32print`.
- Dashboard Flask untuk memilih printer dan mengatur opsi server.
- Dukungan printer lokal maupun network share.
- Mode cetak native, hybrid, dan legacy full-raster.
- Pemrosesan image ESC/POS dengan format raster 1-bit.
- Pemisahan beberapa receipt dan cut command pada legacy full-raster.
- History print dan fitur reprint.
- Logging dengan rotating log handler.
- Dapat dijalankan sebagai aplikasi desktop/tray atau Windows Service.

## Persyaratan

- Windows.
- Python 3.10 atau lebih baru.
- Printer sudah terpasang dan terlihat di **Windows Printers & scanners**.
- Dependensi Python:

```bash
pip install flask waitress pywin32 pillow pystray
```

Untuk membuat executable dan installer, tambahkan:

```bash
pip install pyinstaller
```

Inno Setup 6 diperlukan jika ingin menjalankan proses build installer.

## Menjalankan dari Source

Clone repository:

```bash
git clone https://github.com/oalahbro/EPP-network-print-server.git
cd EPP-network-print-server
```

Jalankan aplikasi:

```bash
python epp.py
```

Dashboard tersedia di:

```text
http://localhost:5000
```

Print socket secara default tersedia di port `9100`.

> Aplikasi menggunakan modul Windows seperti `win32print` dan `pystray`, sehingga tidak ditujukan untuk Linux atau macOS.

## Konfigurasi

Contoh konfigurasi printer-neutral yang disarankan untuk instalasi baru adalah berikut. Pilih printer melalui dashboard setelah server dijalankan:

> Catatan: `conf.json` pada development machine dapat berisi printer dan mode lokal. Sebelum membuat installer portable, pastikan file yang dikirim installer menggunakan nilai printer-neutral di bawah ini.

```json
{
    "DEFAULT": "",
    "PRINTER_NAME": "",
    "PORT": 9100,
    "FLASK_PORT": 5000,
    "MAX_REPRINT": 7,
    "CUT_MODE": "default",
    "RASTER_FONT_SIZE": 24,
    "RASTER_MAX_WIDTH": 576,
    "RASTER_FONT": "",
    "PRINT_MODE": "native"
}
```

Konfigurasi aktif yang dipakai saat development tidak harus sama dengan template di atas. Jangan menyalin konfigurasi printer lokal ke installer untuk mesin lain.

Contoh template lengkap:

```json
{
    "DEFAULT": "",
    "PRINTER_NAME": "",
    "PORT": 9100,
    "FLASK_PORT": 5000,
    "MAX_REPRINT": 7,
    "CUT_MODE": "default",
    "RASTER_FONT_SIZE": 24,
    "RASTER_MAX_WIDTH": 576,
    "RASTER_FONT": "",
    "PRINT_MODE": "native"
}
```

Untuk development, nilai `PRINT_MODE`, printer, dan raster dapat disesuaikan dari dashboard.

Konfigurasi printer-neutral minimal:

```json
{
    "DEFAULT": "",
    "PRINTER_NAME": "",
    "PORT": 9100,
    "FLASK_PORT": 5000,
    "MAX_REPRINT": 7,
    "CUT_MODE": "default",
    "RASTER_FONT_SIZE": 24,
    "RASTER_MAX_WIDTH": 576,
    "RASTER_FONT": "",
    "PRINT_MODE": "native"
}
```

Nama printer yang dipilih disimpan **persis seperti queue yang dikembalikan Windows**. Aplikasi tidak lagi menambahkan hostname komputer secara otomatis, sehingga format berikut dapat digunakan sesuai nama yang terdaftar di Windows:

```text
DRIVER CLIENT
\\SERVER-NAME\THERMAL-PRINTER
```

### Opsi konfigurasi

| Key | Keterangan |
|---|---|
| `DEFAULT` | Nama printer yang ditampilkan sebagai pilihan default di dashboard. |
| `PRINTER_NAME` | Nama queue printer yang digunakan untuk mencetak. Disimpan apa adanya. |
| `PORT` | Port socket print server. Default `9100`. |
| `FLASK_PORT` | Nilai konfigurasi dashboard. Saat ini dashboard dijalankan pada port `5000`. |
| `MAX_REPRINT` | Batas reprint per job. Nilai `0` menonaktifkan reprint. |
| `CUT_MODE` | `default`, `full`, `partial`, atau `none`. |
| `PRINT_MODE` | `native`, `hybrid`, atau `legacy_full_raster`. |
| `RASTER_FONT_SIZE` | Ukuran font raster, server-side dibatasi `12–48`. |
| `RASTER_MAX_WIDTH` | Lebar raster dalam dots, server-side dibatasi `192–1200`. |
| `RASTER_FONT` | Path file font opsional. Kosong berarti auto-detect. |

## Mode Cetak

### Native

Mode default dan paling aman untuk printer yang sudah dapat memproses ESC/POS secara langsung. Payload sumber diteruskan tanpa konversi raster.

Gunakan mode ini jika printer dapat mencetak karakter dan command ESC/POS dari aplikasi POS dengan benar.

### Hybrid

Mode hybrid mempertahankan command/image ESC/POS yang sudah ada dan merasterisasi text dengan byte non-ASCII yang tidak dapat dikirim langsung ke printer.

Mode ini berguna untuk karakter Unicode, CJK, atau karakter khusus yang tidak tersedia pada code page printer.

Untuk receipt yang diproduksi oleh POS project ini, decoder `GS !` pada jalur raster mengikuti konvensi producer saat ini: nilai `0x01` digunakan sebagai ukuran normal. Mode native tetap meneruskan byte `GS !` asli tanpa mengubahnya.

### Legacy Full Raster

Mode ini merender receipt menjadi image raster 1-bit sebelum dikirim ke printer. Opsi ini menyediakan kompatibilitas untuk printer/driver yang tidak memproses text ESC/POS dengan benar.

Perilaku yang didukung:

- Bit image ESC/POS dipetakan dengan benar: bit `1` berarti pixel hitam.
- Beberapa receipt dalam satu stream dipisahkan pada setiap cut.
- Output multi-receipt dikirim dalam urutan image lalu cut untuk setiap receipt.
- Command `GS !` dirender dengan konvensi producer yang sama seperti hybrid.

Mode full-raster dapat mengubah ukuran payload dan tampilan dibandingkan mode native. Uji dengan printer fisik sebelum digunakan untuk operasional.

## Dashboard

Dashboard memungkinkan pengaturan:

- Default printer.
- Port print socket.
- Batas reprint.
- Cut mode.
- Print conversion mode.
- Font size dan max width raster.
- File font opsional.

Saat **Native** dipilih, pengaturan raster otomatis disembunyikan oleh UI karena tidak digunakan. Saat **Hybrid** atau **Full Raster** dipilih, pengaturan raster ditampilkan kembali.

Nilai raster divalidasi oleh server sebelum disimpan:

- Font size: `12–48`.
- Max width: `192–1200` dots.

Request invalid ditolak dengan HTTP 400 dan tidak menjalankan proses save/restart.

## History dan Data Runtime

History aktif disimpan di:

```text
print_history.json
```

Archive history disimpan di folder:

```text
history/
```

Log utama:

```text
server_log.txt
```

File history aktif sudah diperlakukan sebagai file runtime dan tidak lagi dilacak oleh Git. Jangan memasukkan history, log, preview PNG, atau backup source ke commit aplikasi kecuali memang diperlukan.

## Menjalankan Test

Regression test yang tersedia:

```bash
python -m py_compile epp.py escpos.py legacy_renderer.py tests_escpos.py
python tests_escpos.py
```

Test mencakup antara lain:

- Parser stream native tetap lossless.
- Native mempertahankan payload image.
- Cut replacement tidak mengubah payload image.
- Decoder polarity image legacy.
- Konvensi ukuran `GS !` producer.
- Output multi-receipt berupa `image → cut → image → cut`.

Beberapa fixture test membaca data contoh dari folder `history/` pada development checkout.

## Build Executable dan Installer

Build dilakukan menggunakan script Windows:

```bat
build.bat
```

Script tersebut menjalankan PyInstaller dan kemudian mengompilasi installer menggunakan Inno Setup.

Output utama:

```text
dist\epp\
installer_output\
```

Build ulang setelah source berubah agar executable dan installer memuat versi kode terbaru. File `conf.json` dipasang sebagai konfigurasi eksternal di samping executable; pada instalasi baru, konfigurasi printer harus dipilih melalui dashboard.

## Menjalankan sebagai Windows Service

Installer mendaftarkan service:

```text
EPPrintServer
```

Service menjalankan print socket dan dashboard. Untuk troubleshooting, periksa:

1. Printer terlihat di Windows.
2. Nama printer di dashboard sama dengan queue Windows.
3. `PRINTER_NAME` tidak kosong.
4. Port `9100` belum digunakan aplikasi lain.
5. `server_log.txt` untuk detail error.

## Catatan Deployment

- Installer mempertahankan `conf.json` yang sudah ada saat upgrade.
- Instalasi baru menggunakan konfigurasi printer-neutral, sehingga tidak bergantung pada hostname/share mesin developer.
- Pilih printer dari dashboard sebelum melakukan print pertama.
- Build installer hanya setelah executable PyInstaller selesai dibuat dari source terbaru.

## Lisensi

Proyek ini menggunakan lisensi MIT.
