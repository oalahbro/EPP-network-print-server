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
        })
        .catch(error => console.error("Error fetching logs:", error));
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
function viewJob(jobId) {
    fetch(`/view/${jobId}`)
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {

                // convert hex ke text
                let text = hexToString(data.raw_data);
                let clean = text.replace(/\x1B./g, '')
                    // hapus GS (0x1D) + 1 byte setelahnya
                    .replace(/\x1D./g, '')
                    // hapus control char KECUALI \n (0x0A) dan \r (0x0D)
                    .replace(/[\x00-\x09\x0B-\x0C\x0E-\x1F\x7F]/g, '');
                showModal(clean);
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
    if (!hex) return '';
    let bytes = new Uint8Array(hex.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
    return new TextDecoder().decode(bytes);
}

function deleteJob(jobId) {
    fetch(`/history/delete/${jobId}`, { method: "POST" })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                showNotification("🗑️ Job deleted");
                refreshLogs();
            } else {
                showNotification("❌ " + data.message);
            }
        })
        .catch(error => {
            console.error(error);
            showNotification("❌ Server error");
        });
}

function clearHistory() {
    if (!confirm("Hapus semua history?")) return;
    fetch("/history/clear", { method: "POST" })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                showNotification("🗑️ History cleared");
                refreshLogs();
            } else {
                showNotification("❌ " + data.message);
            }
        })
        .catch(error => {
            console.error(error);
            showNotification("❌ Server error");
        });
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

// Auto-refresh every 10 seconds
setInterval(refreshLogs, 10000);
