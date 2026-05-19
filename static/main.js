function logFixer() {
    document.querySelectorAll(".log-line").forEach(function (el) {
        el.innerHTML = el.innerHTML
            .replace(/ð/g, "🚀")  // Ganti karakter yang salah dengan emoji aslinya
            .replace(/ð/g, "🛠️") // Tambahkan pattern lain jika diperlukan
            .replace(/ð¨ï¸/g, "🖨️") // Tambahkan pattern lain jika diperlukan
            .replace(/ð/g, "🔗") // Tambahkan pattern lain jika diperlukan
            .replace(/â/g, "✅") // Tambahkan pattern lain jika diperlukan
            .replace(/ð¨/g, "🖶") // Tambahkan pattern lain jika diperlukanb'
            .replace(/ð/g, "📃") // Tambahkan pattern lain jika diperlukanb'
            .replace(//g, "\n") // Tambahkan pattern lain jika diperlukan
            .replace(//g, "\n"); // Tambahkan pattern lain jika diperlukan
    });
};

function refreshLogs() {
    fetch(window.location.href) // Mengambil ulang halaman dashboard
        .then(response => response.text())
        .then(html => {
            let parser = new DOMParser();
            let doc = parser.parseFromString(html, "text/html");
            let newLogs = doc.getElementById("logContainer").innerHTML;
            let newQueue = doc.getElementById("queueContainer").innerHTML;

            document.getElementById("queueContainer").innerHTML = newQueue; // Update log
            document.getElementById("logContainer").innerHTML = newLogs; // Update log

            logFixer(); // Panggil ulang fungsi untuk memperbaiki karakter

            let picker = document.getElementById("historyDate");
            let wrap = document.getElementById("historyTableWrap");
            let today = wrap ? wrap.getAttribute("data-today") : null;
            if (picker && today && picker.value !== today) {
                loadHistoryForDate(picker.value);
            }
        })
        .catch(error => console.error("Error fetching logs:", error));
}

function loadHistoryForDate(dateStr) {
    fetch(`/history/archive/${dateStr}`)
        .then(r => r.json())
        .then(data => {
            if (data.status === "success") {
                renderHistoryTable(data.history, dateStr);
            } else {
                showNotification("❌ Gagal load history");
            }
        })
        .catch(err => {
            console.error(err);
            showNotification("❌ Server error");
        });
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function renderHistoryTable(history, dateStr) {
    let wrap = document.getElementById("historyTableWrap");
    if (!wrap) return;
    wrap.setAttribute("data-date", dateStr);
    let today = wrap.getAttribute("data-today");
    let maxReprint = parseInt(wrap.getAttribute("data-max-reprint") || "0", 10);
    let isToday = dateStr === today;

    let rows = history.map((job, idx) => {
        let ts = (job.timestamp || "").split(".")[0];
        let printCount = job.print_count || 0;
        let countStyle = (printCount !== 0 && printCount >= maxReprint)
            ? ' style="color: red; font-weight: bold;"'
            : "";
        let viewArg = isToday ? `${job.id}` : `${job.id}, '${dateStr}'`;
        let reprintBtn = (isToday && maxReprint > printCount)
            ? `<button type="button" title="Reprint Receipt" onclick="reprint(${job.id})">🖨️</button>`
            : "";
        return `<tr>
            <td>${idx + 1}</td>
            <td>${escapeHtml(ts)}</td>
            <td>${escapeHtml(job.printer || "")}</td>
            <td${countStyle}>${printCount} Times</td>
            <td style="display: flex; gap: 5px">
                <button type="button" title="View Receipt" onclick="viewJob(${viewArg})">👁️</button>
                ${reprintBtn}
            </td>
        </tr>`;
    }).join("");

    wrap.innerHTML = `<table width="100%" border="1" cellpadding="5" style="border-collapse: collapse;">
        <tr>
            <th>ID</th>
            <th>Time</th>
            <th>Printer</th>
            <th>Reprint Count</th>
            <th>Action</th>
        </tr>
        ${rows}
    </table>`;
}

function showTab(tabName) {
    const logsTab = document.getElementById("logsTab");
    const historyTab = document.getElementById("historyTab");

    if (tabName === "logs") {
        logsTab.style.display = "block";
        historyTab.style.display = "none";
    } else {
        logsTab.style.display = "none";
        historyTab.style.display = "block";
    }
}

function reprint(jobId) {
    fetch(`/reprint/${jobId}`, {
        method: "POST"
    })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                showNotification("✅ Reprint berhasil!");
                refreshLogs()
            } else {
                showNotification("Gagal ❌ : " + data.message);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showNotification("❌ Server error!");
        });
}
function viewJob(jobId, dateStr) {
    let url = `/view/${jobId}`;
    if (dateStr) {
        url += `?date=${encodeURIComponent(dateStr)}`;
    }
    fetch(url)
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                let container = document.getElementById("modalContent");
                container.innerHTML = '';

                // Tampilkan gambar (logo struk)
                if (data.images && data.images.length > 0) {
                    data.images.forEach(b64 => {
                        let img = document.createElement("img");
                        img.src = "data:image/png;base64," + b64;
                        img.style.maxWidth = "100%";
                        img.style.display = "block";
                        img.style.marginBottom = "8px";
                        container.appendChild(img);
                    });
                }

                // Tampilkan teks struk
                let text = hexToString(data.raw_data);
                let clean = text.replace(/\x1B./g, '')
                    .replace(/\x1D./g, '')
                    .replace(/[\x00-\x09\x0B-\x0C\x0E-\x1F\x7F]/g, '');
                let pre = document.createElement("pre");
                pre.textContent = clean;
                pre.style.margin = "0";
                container.appendChild(pre);

                document.getElementById("modalOverlay").style.display = "flex";
            } else {
                showNotification("Data tidak ditemukan");
            }
        })
        .catch(error => {
            console.error(error);
            showNotification("Server error");
        });
}

function hexToString(hex) {
    let bytes = new Uint8Array(hex.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
    return new TextDecoder().decode(bytes);
}

function showModal(content) {
    document.getElementById("modalContent").textContent = content;
    document.getElementById("modalOverlay").style.display = "flex";
}

function closeModal() {
    document.getElementById("modalOverlay").style.display = "none";
}
document.getElementById("modalOverlay").addEventListener("click", function (e) {
    if (e.target === this) {
        closeModal();
    }
});
function showNotification(message) {
    let notif = document.createElement("div");
    notif.innerText = message;
    notif.classList.add("toast", "glass");

    document.body.appendChild(notif);

    setTimeout(() => {
        notif.remove();
    }, 2000);
}
document.addEventListener("DOMContentLoaded", logFixer);