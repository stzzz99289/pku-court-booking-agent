// Vanilla JS — wires up the single "Query orders" button on the Users tab,
// polls /api/jobs/{id} until the all-users fetch completes, then renders one
// combined table sorted by use_date descending.

const POLL_INTERVAL_MS = 1000;

// Delegated click handler — works regardless of script-load timing or
// whether DOMContentLoaded has already fired by the time we register.
document.addEventListener("click", (e) => {
  const queryBtn = e.target.closest("#query-orders-btn");
  if (queryBtn) queryAllOrders(queryBtn);
});

// Last fetched orders from the most recent /api/orders/refresh_all run; the
// venue dropdown re-renders against this in-memory list so toggling the
// filter is instant and never re-hits the site.
let lastFetchedOrders = [];

document.addEventListener("change", (e) => {
  const sel = e.target.closest("#venue-filter");
  if (sel) renderOrderCards(lastFetchedOrders);
});

function _initOnceReady() {
  const form = document.getElementById("booking-form");
  if (form) initBookingForm(form);
  if (document.getElementById("schedule-log")) initScheduleTab();
}
// Actual `_initOnceReady()` invocation is deferred to the very bottom of the
// file so that all `const`s (e.g. SCHEDULE_COUNTDOWN_MS) are initialized
// before init helpers run. See `_runInit` at end of file.

async function queryAllOrders(btn) {
  const status = document.getElementById("orders-status");
  const logEl = document.getElementById("orders-log");
  const resultsEl = document.getElementById("orders-results");
  btn.disabled = true;
  btn.textContent = "Starting…";
  status.innerHTML = `<span class="badge run">running</span>`;
  logEl.style.display = "";
  logEl.textContent = "";
  resultsEl.innerHTML = "";
  try {
    const resp = await fetch("/api/orders/refresh_all", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({limit: 10}),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const {job_id} = await resp.json();
    btn.textContent = "Running…";
    pollAllOrdersJob(job_id, btn);
  } catch (err) {
    status.innerHTML = `<span class="badge err">failed</span>`;
    logEl.textContent += `[error] ${err.message}\n`;
    btn.disabled = false;
    btn.textContent = "Query orders";
  }
}

async function pollAllOrdersJob(jobId, btn) {
  const status = document.getElementById("orders-status");
  const logEl = document.getElementById("orders-log");
  let logOffset = 0;
  while (true) {
    let job;
    try {
      const r = await fetch(`/api/jobs/${jobId}?log_offset=${logOffset}`);
      job = await r.json();
    } catch (err) {
      logEl.textContent += `[poll error] ${err.message}\n`;
      break;
    }
    if (job.logs && job.logs.length) {
      logEl.textContent += job.logs.join("\n") + "\n";
      logEl.scrollTop = logEl.scrollHeight;
      logOffset = job.log_total;
    }
    status.innerHTML = renderStatusBadge(job.status);
    if (job.status === "succeeded") {
      lastFetchedOrders = job.result?.orders || [];
      renderOrderCards(lastFetchedOrders);
      break;
    }
    if (job.status === "failed") {
      logEl.textContent += `[failed] ${job.error}\n`;
      break;
    }
    await sleep(POLL_INTERVAL_MS);
  }
  btn.disabled = false;
  btn.textContent = "Query orders";
}

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function renderOrderCards(orders) {
  const target = document.getElementById("orders-results");
  const venueSel = document.getElementById("venue-filter");
  const venueFilter = venueSel ? venueSel.value : "";
  const filtered = venueFilter
    ? orders.filter((o) => (o.venue ?? "") === venueFilter)
    : orders;
  if (!filtered.length) {
    const note = venueFilter
      ? `(no paid orders matching venue "${escapeHtml(venueFilter)}")`
      : "(no paid orders)";
    target.innerHTML = `<p class="hint">${note}</p>`;
    return;
  }
  const cards = filtered.map((o) => `
    <article class="order-card">
      <div class="order-card-heading">
        <span class="order-field-label">Order ID</span>
        <strong class="order-number">${escapeHtml(o.order_no ?? "—")}</strong>
      </div>
      <div class="order-primary-info">
        <div>
          <span class="order-field-label">Use date</span>
          <strong>${escapeHtml(o.use_date ?? "—")}</strong>
        </div>
        <div>
          <span class="order-field-label">Court &amp; time</span>
          <strong>${escapeHtml(o.court_and_time ?? "—")}</strong>
        </div>
      </div>
      <details class="order-details">
        <summary>Other information</summary>
        <dl>
          <div><dt>User</dt><dd>${escapeHtml(o.user ?? "—")}</dd></div>
          <div><dt>Venue</dt><dd>${escapeHtml(o.venue ?? "—")}</dd></div>
          <div><dt>Amount</dt><dd>${escapeHtml(o.amount ?? "—")}</dd></div>
          <div><dt>Status</dt><dd>${escapeHtml(o.order_status ?? "—")}</dd></div>
          <div><dt>Created</dt><dd>${escapeHtml(o.created_at ?? "—")}</dd></div>
        </dl>
      </details>
    </article>
  `).join("");
  target.innerHTML = `<div class="order-list">${cards}</div>`;
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
  pollBookingAvailability();

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
  bookingRunInFlight = true;
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
    bookingRunInFlight = false;
  }
}

// Poll /api/schedule/status so the "Run now" button greys out while the
// scheduled task is running or in its no-test prep window. Server enforces
// the same rule (HTTP 409) — this is just a UX hint.
const BOOKING_AVAIL_POLL_MS = 5000;
let bookingRunInFlight = false;

async function pollBookingAvailability() {
  try {
    const r = await fetch("/api/schedule/status");
    const data = await r.json();
    applyBookingAvailability(data);
  } catch (err) {
    // ignore; try again next tick
  }
  setTimeout(pollBookingAvailability, BOOKING_AVAIL_POLL_MS);
}

function applyBookingAvailability(data) {
  const btn = document.getElementById("run-btn");
  const msg = document.getElementById("run-msg");
  if (!btn) return;
  if (bookingRunInFlight) return;  // don't fight the in-flight handler
  const blocked = !!data.no_test_window_active;
  btn.disabled = blocked;
  if (blocked) {
    if (data.state === "running") {
      btn.title = "scheduled task is running";
      msg.textContent = "scheduled task is running — test runs paused";
    } else {
      const fire = data.next_fire ? new Date(data.next_fire * 1000).toLocaleTimeString() : "soon";
      btn.title = `scheduled task fires at ${fire}; test runs blocked in the prep window`;
      msg.textContent = `test runs paused until after ${fire}`;
    }
  } else {
    btn.title = "";
    if (msg.textContent.startsWith("test runs paused") || msg.textContent.startsWith("scheduled task")) {
      msg.textContent = "";
    }
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
  bookingRunInFlight = false;
}

function renderStatusBadge(status) {
  const cls = status === "succeeded" ? "ok" : status === "failed" ? "err" : "run";
  return `<span class="badge ${cls}">${status}</span>`;
}

// ---------- Scheduled Task tab ----------

const SCHEDULE_POLL_RUNNING_MS = 1000;
const SCHEDULE_POLL_WAITING_MS = 5000;
const SCHEDULE_COUNTDOWN_MS = 1000;

let scheduleNextFireEpoch = null;

function initScheduleTab() {
  pollSchedule();
  setInterval(updateCountdown, SCHEDULE_COUNTDOWN_MS);
}

async function pollSchedule() {
  try {
    const r = await fetch("/api/schedule/status");
    const data = await r.json();
    renderScheduleStatus(data);
    const next = data.state === "running" ? SCHEDULE_POLL_RUNNING_MS : SCHEDULE_POLL_WAITING_MS;
    setTimeout(pollSchedule, next);
  } catch (err) {
    setTimeout(pollSchedule, SCHEDULE_POLL_WAITING_MS);
  }
}

function renderScheduleStatus(data) {
  const badge = document.getElementById("schedule-status-badge");
  badge.textContent = data.state;
  badge.className = "badge " + (data.state === "running" ? "run" : "ok");

  scheduleNextFireEpoch = data.next_fire || null;
  const nextFireEl = document.getElementById("schedule-next-fire");
  nextFireEl.textContent = scheduleNextFireEpoch
    ? new Date(scheduleNextFireEpoch * 1000).toLocaleString()
    : "(computing…)";

  const log = document.getElementById("schedule-log");
  const text = (data.logs && data.logs.length) ? data.logs.join("\n") : "(no run yet)";
  // Preserve scroll position when user has scrolled up to read.
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 5;
  log.textContent = text;
  if (atBottom) log.scrollTop = log.scrollHeight;

  renderLastRunSummary(data);
  updateCountdown();
}

function renderLastRunSummary(data) {
  const target = document.getElementById("schedule-last-run-summary");
  const lr = data.last_run;
  if (!lr) { target.innerHTML = ""; return; }
  const finished = lr.finished_at ? new Date(lr.finished_at * 1000).toLocaleString() : "—";
  const ok = lr.any_success ? "ok" : "err";
  const head = lr.any_success ? "Last run: at least one worker succeeded"
                              : "Last run: no worker succeeded";
  const rows = (lr.results || []).map((r, i) => `
    <li>worker ${i + 1}: <strong>${r.success ? "OK" : "FAIL"}</strong> — ${escapeHtml(r.message || "")}</li>
  `).join("");
  target.innerHTML = `<div class="result-box ${ok}">
    <strong>${head}</strong>
    <div class="hint">finished ${escapeHtml(finished)} · duration ${lr.duration_s ?? "?"}s</div>
    ${rows ? `<ul class="result-list">${rows}</ul>` : ""}
  </div>`;
}

function updateCountdown() {
  const el = document.getElementById("schedule-countdown");
  if (!el) return;
  if (!scheduleNextFireEpoch) { el.textContent = "—"; return; }
  let remaining = Math.floor(scheduleNextFireEpoch - Date.now() / 1000);
  if (remaining < 0) remaining = 0;
  const h = Math.floor(remaining / 3600);
  const m = Math.floor((remaining % 3600) / 60);
  const s = remaining % 60;
  el.textContent = `${h}h ${String(m).padStart(2,"0")}m ${String(s).padStart(2,"0")}s`;
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

// Kick off init AFTER all const/function declarations above have been
// evaluated. With `<script defer>`, parsing is already complete here.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", _initOnceReady);
} else {
  _initOnceReady();
}
