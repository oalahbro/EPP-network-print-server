// ==================== PRINTER MANAGEMENT ====================

function loadPrinters() {
    fetch('/api/printers')
        .then(r => r.json())
        .then(data => {
            renderPrinterCards(data.printers, data.available);
        })
        .catch(err => {
            console.error("Error loading printers:", err);
            document.getElementById("printerList").innerHTML = '<div class="loading">Gagal memuat printer</div>';
        });
}

function renderPrinterCards(printers, availablePrinters) {
    const container = document.getElementById("printerList");
    container.innerHTML = "";

    if (printers.length === 0) {
        container.innerHTML = '<div class="loading">Belum ada printer. Klik + untuk menambahkan.</div>';
        return;
    }

    printers.forEach(printer => {
        const card = document.createElement("div");
        card.className = "printer-card";
        card.setAttribute("data-printer-id", printer.id);

        let options = availablePrinters.map(p => {
            const selected = p === printer.printer_name || printer.printer_name.endsWith("\\" + p) ? "selected" : "";
            return `<option value="${p}" ${selected}>${p}</option>`;
        }).join("");

        card.innerHTML = `
            <div class="card-header">
                <span class="status-dot ${printer.running ? 'running' : 'stopped'}"></span>
                <strong>${printer.name}</strong>
                <span class="port-badge">Port: ${printer.port}</span>
            </div>
            <div class="card-body">
                <div class="card-field">
                    <label>Printer:</label>
                    <select class="card-select" data-field="printer_name">
                        <option value="">-- Pilih Printer --</option>
                        ${options}
                    </select>
                </div>
                <div class="card-row">
                    <div class="card-field">
                        <label>Port:</label>
                        <input type="number" class="card-input" data-field="port" value="${printer.port}" min="1" max="65535">
                    </div>
                    <div class="card-field">
                        <label>Max Reprint:</label>
                        <input type="number" class="card-input" data-field="max_reprint" value="${printer.max_reprint}" min="0">
                    </div>
                </div>
            </div>
            <div class="card-actions">
                <button class="glass card-save-btn" onclick="savePrinter('${printer.id}')">Save</button>
                <button class="glass card-delete-btn" onclick="deletePrinter('${printer.id}', '${printer.name}')">Delete</button>
            </div>
        `;

        container.appendChild(card);
    });
}

function addPrinter() {
    fetch('/api/system-printers')
        .then(r => r.json())
        .then(data => {
            const printers = data.printers;
            const firstName = printers.length > 0 ? printers[0] : "";

            // Cari port yang belum terpakai
            const existingPorts = Array.from(document.querySelectorAll('[data-field="port"]')).map(el => parseInt(el.value));
            let newPort = 9100;
            while (existingPorts.includes(newPort)) {
                newPort++;
            }

            fetch('/api/printers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: firstName || "New Printer",
                    printer_name: firstName,
                    port: newPort,
                    max_reprint: 3
                })
            })
                .then(r => r.json())
                .then(result => {
                    if (result.status === "success") {
                        showNotification("Printer ditambahkan!");
                        loadPrinters();
                    } else {
                        showNotification("Gagal: " + result.message);
                    }
                })
                .catch(err => {
                    console.error(err);
                    showNotification("Server error!");
                });
        });
}

function savePrinter(printerId) {
    const card = document.querySelector(`[data-printer-id="${printerId}"]`);
    if (!card) return;

    const printerName = card.querySelector('[data-field="printer_name"]').value;
    const port = card.querySelector('[data-field="port"]').value;
    const maxReprint = card.querySelector('[data-field="max_reprint"]').value;

    if (!printerName) {
        showNotification("Pilih printer terlebih dahulu!");
        return;
    }

    fetch(`/api/printers/${printerId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name: printerName,
            printer_name: printerName,
            port: parseInt(port),
            max_reprint: parseInt(maxReprint)
        })
    })
        .then(r => r.json())
        .then(result => {
            if (result.status === "success") {
                showNotification("Printer disimpan!");
                loadPrinters();
            } else {
                showNotification("Gagal: " + result.message);
            }
        })
        .catch(err => {
            console.error(err);
            showNotification("Server error!");
        });
}

function deletePrinter(printerId, name) {
    if (!confirm(`Hapus printer "${name}"?`)) return;

    fetch(`/api/printers/${printerId}`, {
        method: 'DELETE'
    })
        .then(r => r.json())
        .then(result => {
            if (result.status === "success") {
                showNotification("Printer dihapus!");
                loadPrinters();
            } else {
                showNotification("Gagal: " + result.message);
            }
        })
        .catch(err => {
            console.error(err);
            showNotification("Server error!");
        });
}

// ==================== LOG & HISTORY ====================

function logFixer() {
    document.querySelectorAll(".log-line").forEach(function (el) {
        el.innerHTML = el.innerHTML
            .replace(/\u00f0/g, "")
            .replace(/\n/g, "\n");
    });
}

function refreshLogs() {
    fetch(window.location.href)
        .then(response => response.text())
        .then(html => {
            let parser = new DOMParser();
            let doc = parser.parseFromString(html, "text/html");
            let newLogs = doc.getElementById("logContainer").innerHTML;
            let newQueue = doc.getElementById("queueContainer").innerHTML;

            document.getElementById("queueContainer").innerHTML = newQueue;
            document.getElementById("logContainer").innerHTML = newLogs;

            logFixer();
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
    fetch(`/reprint/${jobId}`, { method: "POST" })
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                showNotification("Reprint berhasil!");
                refreshLogs();
            } else {
                showNotification("Gagal: " + data.message);
            }
        })
        .catch(error => {
            console.error("Error:", error);
            showNotification("Server error!");
        });
}

function viewJob(jobId) {
    fetch(`/view/${jobId}`)
        .then(response => response.json())
        .then(data => {
            if (data.status === "success") {
                let text = hexToString(data.raw_data);
                let clean = text.replace(/\x1B./g, '')
                    .replace(/\x1D./g, '')
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
    let bytes = new Uint8Array(hex.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
    return new TextDecoder().decode(bytes);
}

// ==================== MODAL ====================

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

// ==================== NOTIFICATIONS ====================

function showNotification(message) {
    let notif = document.createElement("div");
    notif.innerText = message;
    notif.classList.add("toast", "glass");
    document.body.appendChild(notif);
    setTimeout(() => { notif.remove(); }, 2000);
}

// ==================== INIT ====================

document.addEventListener("DOMContentLoaded", function () {
    logFixer();
    loadPrinters();
});
