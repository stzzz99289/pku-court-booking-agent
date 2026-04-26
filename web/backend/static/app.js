// Vanilla JS — wires up the "Refresh orders" buttons on the Users tab and
// polls /api/jobs/{id} until the orders fetch completes, then renders the table.

const POLL_INTERVAL_MS = 1000;

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".refresh-orders-btn");
  if (btn) refreshOrders(btn);
});

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("booking-form");
  if (form) initBookingForm(form);
});

async function refreshOrders(btn) {
  const user = btn.dataset.user;
  btn.disabled = true;
  btn.textContent = "Starting…";
  ensureUserOrdersBlock(user, "running", "[launching browser]");
  try {
    const resp = await fetch("/api/orders/refresh", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({user, limit: 10}),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const {job_id} = await resp.json();
    btn.textContent = "Running…";
    pollJob(user, job_id, btn);
  } catch (err) {
    ensureUserOrdersBlock(user, "failed", `[error] ${err.message}`);
    btn.disabled = false;
    btn.textContent = "Refresh orders";
  }
}

async function pollJob(user, jobId, btn) {
  let logOffset = 0;
  while (true) {
    let job;
    try {
      const r = await fetch(`/api/jobs/${jobId}?log_offset=${logOffset}`);
      job = await r.json();
    } catch (err) {
      ensureUserOrdersBlock(user, "failed", `[poll error] ${err.message}`);
      break;
    }
    if (job.logs && job.logs.length) {
      appendLogs(user, job.logs);
      logOffset = job.log_total;
    }
    setBlockStatus(user, job.status);
    if (job.status === "succeeded") {
      renderOrdersTable(user, job.result?.orders || []);
      break;
    }
    if (job.status === "failed") {
      appendLogs(user, [`[failed] ${job.error}`]);
      break;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  btn.disabled = false;
  btn.textContent = "Refresh orders";
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function ensureUserOrdersBlock(user, status, initialLog) {
  const root = document.getElementById("orders-results");
  let block = root.querySelector(`[data-user-block="${user}"]`);
  if (!block) {
    block = document.createElement("div");
    block.className = "user-orders panel";
    block.dataset.userBlock = user;
    block.innerHTML = `
      <h3>${user} <span class="badge run js-status">${status}</span></h3>
      <div class="log-tail js-log"></div>
      <div class="js-table"></div>
    `;
    root.prepend(block);
  } else {
    block.querySelector(".js-log").textContent = "";
    block.querySelector(".js-table").innerHTML = "";
  }
  if (initialLog) appendLogs(user, [initialLog]);
  setBlockStatus(user, status);
  return block;
}

function setBlockStatus(user, status) {
  const block = document.querySelector(`[data-user-block="${user}"]`);
  if (!block) return;
  const badge = block.querySelector(".js-status");
  badge.textContent = status;
  badge.className = "badge js-status " + (
    status === "succeeded" ? "ok" :
    status === "failed" ? "err" : "run"
  );
}

function appendLogs(user, lines) {
  const block = document.querySelector(`[data-user-block="${user}"]`);
  if (!block) return;
  const log = block.querySelector(".js-log");
  log.textContent += lines.join("\n") + "\n";
  log.scrollTop = log.scrollHeight;
}

function renderOrdersTable(user, orders) {
  const block = document.querySelector(`[data-user-block="${user}"]`);
  if (!block) return;
  const target = block.querySelector(".js-table");
  if (!orders.length) {
    target.innerHTML = `<p class="hint">(no paid orders)</p>`;
    return;
  }
  const cols = ["order_no", "venue", "use_date", "court_and_time", "amount", "order_status", "created_at"];
  const head = cols.map((c) => `<th>${c}</th>`).join("");
  const rows = orders.map((o) =>
    "<tr>" + cols.map((c) => `<td>${escapeHtml(o[c] ?? "")}</td>`).join("") + "</tr>"
  ).join("");
  target.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

// ---------- Run Booking tab ----------

function initBookingForm(form) {
  const list = document.getElementById("priority-list");
  const pool = form.querySelector(".hour-pool");
  const prefill = JSON.parse(list.dataset.prefill || "[]");
  prefill.forEach((h) => addHour(h));
  syncChipStates();

  pool.addEventListener("click", (e) => {
    const chip = e.target.closest(".hour-chip");
    if (!chip) return;
    const h = chip.dataset.hour;
    if (currentHours().includes(h)) removeHour(h);
    else addHour(h);
    syncChipStates();
  });

  list.addEventListener("click", (e) => {
    const li = e.target.closest("li[data-hour]");
    if (!li) return;
    removeHour(li.dataset.hour);
    syncChipStates();
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submitBooking(form);
  });
}

function currentHours() {
  return Array.from(document.querySelectorAll("#priority-list li"))
    .map((li) => li.dataset.hour);
}

function addHour(h) {
  const list = document.getElementById("priority-list");
  const li = document.createElement("li");
  li.dataset.hour = h;
  li.innerHTML = `<span class="prio-num"></span> ${h}:00 <span class="x">×</span>`;
  list.appendChild(li);
  renumber();
}

function removeHour(h) {
  const li = document.querySelector(`#priority-list li[data-hour="${h}"]`);
  if (li) li.remove();
  renumber();
}

function renumber() {
  document.querySelectorAll("#priority-list li .prio-num").forEach((el, i) => {
    el.textContent = `${i + 1}.`;
  });
}

function syncChipStates() {
  const picked = new Set(currentHours());
  document.querySelectorAll(".hour-chip").forEach((chip) => {
    chip.classList.toggle("picked", picked.has(chip.dataset.hour));
  });
}

async function submitBooking(form) {
  const btn = document.getElementById("run-btn");
  const msg = document.getElementById("run-msg");
  const hours = currentHours();
  if (hours.length === 0) {
    msg.textContent = "pick at least one hour";
    return;
  }
  const data = {
    user: form.user.value,
    venue_id: parseInt(form.venue_id.value, 10),
    date: form.date.value,
    start_time_list: hours,
  };
  btn.disabled = true;
  btn.textContent = "Starting…";
  msg.textContent = "";
  document.getElementById("run-status").innerHTML = "";
  document.getElementById("run-log").textContent = "[launching]\n";
  document.getElementById("run-result").innerHTML = "";

  try {
    const r = await fetch("/api/bookings/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: `HTTP ${r.status}`}));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    const {job_id} = await r.json();
    btn.textContent = "Running…";
    pollBookingJob(job_id, btn, msg);
  } catch (err) {
    msg.textContent = err.message;
    btn.disabled = false;
    btn.textContent = "Run now";
  }
}

async function pollBookingJob(jobId, btn, msg) {
  const log = document.getElementById("run-log");
  const status = document.getElementById("run-status");
  const resultBox = document.getElementById("run-result");
  log.textContent = "";
  let logOffset = 0;

  while (true) {
    let job;
    try {
      const r = await fetch(`/api/jobs/${jobId}?log_offset=${logOffset}`);
      job = await r.json();
    } catch (err) {
      log.textContent += `[poll error] ${err.message}\n`;
      break;
    }
    if (job.logs && job.logs.length) {
      log.textContent += job.logs.join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
      logOffset = job.log_total;
    }
    status.innerHTML = renderStatusBadge(job.status);
    if (job.status === "succeeded" || job.status === "failed") {
      resultBox.innerHTML = renderBookingResult(job);
      break;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  btn.disabled = false;
  btn.textContent = "Run now";
  msg.textContent = "";
}

function renderStatusBadge(status) {
  const cls = status === "succeeded" ? "ok" : status === "failed" ? "err" : "run";
  return `<span class="badge ${cls}">${status}</span>`;
}

function renderBookingResult(job) {
  const r = job.result || {};
  if (job.status === "failed") {
    return `<div class="result-box err"><strong>Job error:</strong> ${escapeHtml(job.error || "")}</div>`;
  }
  const ok = r.success ? "ok" : "err";
  const head = r.success ? "Booking succeeded" : "Booking failed";
  const details = r.details && Object.keys(r.details).length
    ? `<pre class="result-details">${escapeHtml(JSON.stringify(r.details, null, 2))}</pre>`
    : "";
  return `<div class="result-box ${ok}">
    <strong>${head}</strong>
    <div>${escapeHtml(r.message || "")}</div>
    ${details}
  </div>`;
}
