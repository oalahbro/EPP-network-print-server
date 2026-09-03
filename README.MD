# Print Server

Print Server ini adalah aplikasi berbasis Python yang berfungsi sebagai server cetak menggunakan Flask dan socket server. Proyek ini memungkinkan pengguna untuk mengirim dokumen ke printer melalui jaringan, mirip dengan CUPS (Common UNIX Printing System), tetapi dibuat khusus untuk lingkungan Windows dengan `win32print`.

Aplikasi ini **hanya mendukung printer thermal** yang menggunakan pengkodean **ESC/POS**, yang umum digunakan dalam pencetakan struk atau sistem kasir. Server ini dapat diarahkan ke **port driver printer Windows** yang menggunakan driver **POS80 Universal** atau **Text/Generic Printer**, sehingga kompatibel dengan berbagai jenis printer thermal yang mendukung ESC/POS.

## 🚀 Cara Menggunakan

### 1. **Clone Repository**

```bash
git clone https://github.com/username/repository-name.git
cd repository-name
```

### 2. **Install Dependensi**

Gunakan `pip` untuk menginstal dependensi yang diperlukan:

```bash
pip install flask waitress pywin32 pillow pystray
```

### 3. **Konfigurasi**

Edit `conf.json` untuk mengatur printer dan port yang digunakan:

```json
{
  "DEFAULT": "",
  "PRINTER_NAME": "",
  "PORT": 9100
}
```

Pilih printer dari dashboard setelah server dijalankan. Nama printer disimpan persis seperti yang terdaftar di Windows, termasuk printer lokal maupun network share.

### 4. **Menjalankan Server**

Jalankan server dengan perintah berikut:

```bash
python epp.py
```

### 5. **Akses Dashboard**

Buka browser dan akses:

```
http://localhost:5000
```

## 📜 Fitur

- Menerima data cetak melalui socket
- Mengirim data ke printer menggunakan `win32print`
- Menyediakan dashboard berbasis Flask
- Logging dengan sistem rotasi file (`log1.bak`, `log2.bak`, ...)
- Reprint receipt history
- Berfungsi sebagai print spooler alternatif seperti CUPS untuk Windows
- **Didesain khusus untuk printer thermal dengan ESC/POS**
- **Mendukung printer thermal dengan driver POS80 Universal atau Text/Generic Printer di Windows**

## 📌 Catatan

- Pastikan Python sudah terinstal.
- Aplikasi ini hanya berjalan di Windows karena menggunakan `win32print`.
- Mendukung printer regular dan ESC/POS.

## 🛠 Pengembangan & Kontribusi

Jika ingin berkontribusi, fork repository ini dan buat pull request. Jangan lupa untuk menambahkan dokumentasi yang jelas.

## 📄 Lisensi

Proyek ini menggunakan lisensi MIT. Silakan baca `LICENSE` untuk detail lebih lanjut.

---

💻 @oalahbro
