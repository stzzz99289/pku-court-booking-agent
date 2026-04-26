// Vanilla JS — wires up the "Refresh orders" buttons on the Users tab and
// polls /api/jobs/{id} until the orders fetch completes, then renders the table.

const POLL_INTERVAL_MS = 1000;

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".refresh-orders-btn");
  if (btn) refreshOrders(btn);
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
