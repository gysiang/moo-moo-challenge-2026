const money = (v) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd = (v) => (v === null || v === undefined ? "—" : "$" + money(v));
const pct = (v, digits = 2) => (v === null || v === undefined ? "—" : (v * 100).toFixed(digits) + "%");
const signed = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(2));
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clock = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const shortClock = (iso) =>
  iso
    ? new Date(iso).toLocaleString(undefined, {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";

let state = null;
let openDetail = null;
let busy = false;
let cloudState = null;
let cloudOpen = null;
const cloudEvents = new Map();

function toast(message, bad = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 6000);
}

function kpi(label, value, sub, tone) {
  return `<div class="kpi">
    <div class="kpi-label">${esc(label)}</div>
    <div class="kpi-value ${tone || ""}">${value}</div>
    <div class="kpi-sub">${sub || "&nbsp;"}</div>
  </div>`;
}

function renderKpis(s) {
  if (s.has_snapshot === false) {
    document.getElementById("kpis").innerHTML = `<div class="empty" style="grid-column:1/-1">
      No <code>account-5829804.json</code> in this runtime — start the dashboard with
      <code>--runtime</code> pointing at the directory the pipeline writes to (here:
      <code>runtime/demo</code>).</div>`;
    return;
  }
  const k = s.kpi;
  const dayTone = k.day_pnl_pct > 0 ? "pos" : k.day_pnl_pct < 0 ? "neg" : "";
  const totTone = k.total_pnl > 0 ? "pos" : k.total_pnl < 0 ? "neg" : "";
  const totalPnl = `${k.total_pnl > 0 ? "+" : k.total_pnl < 0 ? "-" : ""}${usd(Math.abs(k.total_pnl))}`;
  document.getElementById("kpis").innerHTML = [
    kpi("Equity", usd(k.equity), `peak ${usd(k.peak_equity)}`),
    kpi("Cash", usd(k.cash), `MV ${usd(k.market_value)}`),
    kpi("Day P&L", pct(k.day_pnl_pct), "vs day open", dayTone),
    kpi("Total P&L", totalPnl, `${pct(k.total_pnl_pct)} vs ${usd(k.baseline)}`, totTone),
    kpi("Drawdown", pct(k.drawdown_pct), `max ${pct(k.max_drawdown_pct)}`, k.drawdown_pct < 0 ? "neg" : ""),
    kpi(
      "Usage",
      `${k.positions_used}/${k.positions_limit}`,
      `orders ${k.orders_used}/${k.orders_limit}`
    ),
  ].join("");
}

function renderChart(s) {
  const points = s.equity_series || [];
  const el = document.getElementById("chart");
  const k = s.kpi;
  document.getElementById("equity-legend").innerHTML = `<span>${points.length} pts</span><span>baseline ${usd(k.baseline)}</span>`;
  document.getElementById("chart-foot").innerHTML = `
    <span>current ${usd(k.equity)}</span>
    <span>peak ${usd(k.peak_equity)}</span>
    <span>max dd ${pct(k.max_drawdown_pct)}</span>`;

  if (points.length < 2) {
    el.innerHTML = `<div class="empty">Not enough equity points yet — the series grows as the account snapshot changes (each load appends a point when equity moves).</div>`;
    return;
  }

  const W = 720, H = 220, P = 14;
  const values = points.map((p) => p.equity).concat([k.baseline]);
  let min = Math.min(...values), max = Math.max(...values);
  if (max - min < 1) { max += 1; min -= 1; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const x = (i) => P + (i * (W - 2 * P)) / (points.length - 1);
  const y = (v) => H - P - ((v - min) / (max - min)) * (H - 2 * P);
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(1)},${H - P} L${x(0).toFixed(1)},${H - P} Z`;
  const baseY = y(k.baseline).toFixed(1);
  const up = points[points.length - 1].equity >= points[0].equity;
  const stroke = up ? "#3ddc97" : "#ff6b81";

  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="equity curve">
    <defs><linearGradient id="fill" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0%" stop-color="${stroke}" stop-opacity=".28"/>
      <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
    </linearGradient></defs>
    <line x1="${P}" x2="${W - P}" y1="${baseY}" y2="${baseY}" stroke="#3a4757" stroke-dasharray="4 5" stroke-width="1"/>
    <path d="${area}" fill="url(#fill)"/>
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2" stroke-linejoin="round"/>
    <circle cx="${x(points.length - 1).toFixed(1)}" cy="${y(points[points.length - 1].equity).toFixed(1)}" r="3.5" fill="${stroke}"/>
  </svg>`;
}

function renderPositions(s) {
  const rows = s.positions || [];
  document.getElementById("positions-count").textContent = `${rows.length} open · limit ${s.limits.max_positions}`;
  const head = `<tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg cost</th><th class="num">Market value</th><th class="num">Unrealized</th><th class="num">Weight</th></tr>`;
  const body = rows.length
    ? rows.map((p) => {
        const tone = p.unrealized > 0 ? "pos" : p.unrealized < 0 ? "neg" : "";
        return `<tr>
          <td class="sym">${esc(p.symbol)}</td>
          <td class="num">${p.quantity}</td>
          <td class="num">${money(p.average_cost)}</td>
          <td class="num">${money(p.market_value)}</td>
          <td class="num ${tone}">${signed(p.unrealized)} (${pct(p.unrealized_pct)})</td>
          <td class="num">${pct(p.weight)}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="6" class="empty">No open positions.</td></tr>`;
  document.getElementById("positions").innerHTML = head + body;
}

function renderOrders(s) {
  const rows = s.open_orders || [];
  document.getElementById("orders-count").textContent = `${rows.length} open`;
  const head = `<tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Limit</th><th>State</th></tr>`;
  const body = rows.length
    ? rows.map((o) => `<tr>
        <td class="sym">${esc(o.symbol)}</td>
        <td class="side-${esc((o.side || "").toLowerCase())}">${esc(o.side)}</td>
        <td class="num">${o.quantity ?? "—"}</td>
        <td class="num">${o.limit_price != null ? money(o.limit_price) : "—"}</td>
        <td><span class="pill awaiting">${esc(o.status)}</span>${o.ticket_id ? ` <span class="muted">${esc(o.ticket_id)}</span>` : ""}</td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="empty">No open orders.</td></tr>`;
  document.getElementById("orders").innerHTML = head + body;
}

function renderGuard(s) {
  const g = s.guard || {};
  const banner = g.halted || g.kill_switch
    ? `<div class="banner bad">order flow latched — ${g.kill_switch ? "kill switch" : "halt"}${g.halted && g.kill_switch ? " + halt" : ""}</div>`
    : `<div class="banner ok">order flow normal</div>`;
  document.getElementById("guard").innerHTML = `
    ${banner}
    <div class="line"><span>events</span><span>${g.events ?? 0}</span></div>
    <div class="line"><span>halted</span><span>${g.halted ? "yes" : "no"}</span></div>
    <div class="line"><span>kill switch</span><span>${g.kill_switch ? "yes" : "no"}</span></div>
    <div class="line"><span>approvals valid</span><span>${Object.keys(s.approvals || {}).length}</span></div>
    <div class="line"><span>execution</span><span>manual Guard run only</span></div>`;
}

const pillClass = (status) =>
  status === "READY" ? "ready"
  : status === "AWAITING_APPROVAL" ? "awaiting"
  : status === "APPROVED" ? "approved"
  : status === "APPROVAL_EXPIRED" ? "expired"
  : status === "REJECTED" ? "rejected"
  : "";

function renderDecisions(s) {
  const rows = s.decisions || [];
  const c = s.counts || {};
  document.getElementById("decision-counts").innerHTML = `
    <span>awaiting ${c.awaiting ?? 0}</span><span>approved ${c.approved ?? 0}</span>
    <span>rejected ${c.rejected ?? 0}</span><span>ready ${c.ready ?? 0}</span>`;
  const head = `<tr><th class="col-time">Generated</th><th>Ticket</th><th>Side</th><th>Symbol</th><th class="num">Qty</th><th class="num">Limit</th><th class="num col-cost">Est cost</th><th>Gate</th><th>Status</th><th></th></tr>`;
  if (!rows.length) {
    document.getElementById("decisions").innerHTML = head + `<tr><td colspan="10" class="empty">No cycle reports in <code>runtime/</code> yet.</td></tr>`;
    return;
  }
  const body = rows.map((d) => {
    const key = `${d.cycle}|${d.ticket_id}`;
    const codes = (d.gate_codes || []).join(", ") || "ok";
    const short = (d.gate_codes || []).map((c) => String(c).split("_")[0]).join(", ") || "ok";
    const canApprove = d.status === "AWAITING_APPROVAL";
    const action = canApprove
      ? `<button class="primary" data-approve="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Approve</button>`
      : "";
    const main = `<tr class="row-toggle" data-row="${esc(key)}">
      <td class="col-time">${clock(d.generated_at)}</td>
      <td>${esc(d.ticket_id)}</td>
      <td class="side-${esc((d.side || "").toLowerCase())}">${esc(d.side)}</td>
      <td class="sym">${esc(d.symbol)}</td>
      <td class="num">${d.quantity ?? "—"}</td>
      <td class="num">${d.limit_price != null ? money(d.limit_price) : "—"}</td>
      <td class="num col-cost">${d.est_cost != null ? usd(d.est_cost) : "—"}</td>
      <td title="${esc(codes)}">${esc(short)}</td>
      <td><span class="pill ${pillClass(d.status)}">${esc(d.status)}</span></td>
      <td>${action}</td>
    </tr>`;
    if (openDetail !== key) return main;
    const evidence = (d.evidence || [])
      .map((e) => `<li>${esc(e.kind || "")} · ${esc(e.source || "")} · ${esc(e.id || "")} · ${clock(e.as_of)}</li>`)
      .join("");
    const detail = `<tr class="detail"><td colspan="10">
      <div class="detail-grid">
        <div>
          <h4>Rationale</h4>
          <p>${esc(d.rationale || "—")}</p>
          <h4>Cycle</h4>
          <p>${esc(d.cycle)} · slot ${esc(d.slot || "—")} · ${esc(d.sleeve || "")}</p>
          <h4>Generated / gate</h4>
          <p>${clock(d.generated_at)} · ${esc(codes)}</p>
          ${d.approval_expires_at ? `<h4>Approval expires</h4><p>${clock(d.approval_expires_at)}</p>` : ""}
        </div>
        <div>
          <h4>Evidence</h4>
          <ul class="evidence">${evidence || "<li>none</li>"}</ul>
          ${canApprove ? `<div class="approve-box"><button class="primary" data-approve="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Approve this ticket</button><span class="muted">writes approvals.json · no order sent</span></div>` : ""}
        </div>
      </div>
    </td></tr>`;
    return main + detail;
  }).join("");
  document.getElementById("decisions").innerHTML = head + body;
}

function render(s) {
  document.getElementById("account-id").textContent = s.account_id || "—";
  document.getElementById("mode").innerHTML = s.demo
    ? `DEMO <span class="demo-badge">SYNTHETIC DATA</span>`
    : "LIVE";
  document.getElementById("as-of").textContent = clock(s.as_of);
  renderKpis(s);
  renderChart(s);
  renderPositions(s);
  renderOrders(s);
  renderGuard(s);
  renderDecisions(s);
}

const cloudPill = (status) =>
  status === "running" ? "awaiting"
  : status === "idle" ? "approved"
  : status === "terminated" ? "rejected"
  : status === "rescheduling" ? "expired"
  : "";

function renderCloud(payload) {
  cloudState = payload || {};
  const rows = cloudState.sessions || [];
  const count = document.getElementById("cloud-count");
  const table = document.getElementById("cloud");
  const head = `<tr><th class="col-time">Started</th><th>Agent</th><th>Session</th><th>Status</th><th class="num col-cost">Credits</th></tr>`;

  if (!cloudState.configured) {
    count.textContent = "disabled";
    table.innerHTML = head + `<tr><td colspan="5" class="empty">Cloud session reads are off — set QODER_PAT_TKN in .env and restart. The token stays on the server.</td></tr>`;
    return;
  }
  if (cloudState.error) {
    count.textContent = "error";
    table.innerHTML = head + `<tr><td colspan="5" class="empty">Cloud API: ${esc(cloudState.error)}</td></tr>`;
    return;
  }
  count.textContent = `${rows.length} recent`;
  if (!rows.length) {
    table.innerHTML = head + `<tr><td colspan="5" class="empty">No cloud sessions yet.</td></tr>`;
    return;
  }

  const body = rows.map((s) => {
    const id = s.id || "";
    const main = `<tr class="row-toggle" data-cloud="${esc(id)}">
      <td class="col-time">${shortClock(s.created_at)}</td>
      <td class="sym">${esc(s.agent || "—")}</td>
      <td>${esc(s.title || id)}<div class="muted">${esc(s.deployment_id || "manual run")}</div></td>
      <td><span class="pill ${cloudPill(s.status)}">${esc(s.status || "—")}</span></td>
      <td class="num col-cost">${s.credits != null ? money(s.credits) : "—"}</td>
    </tr>`;
    if (cloudOpen !== id) return main;
    const events = cloudEvents.get(id);
    const listing =
      events === undefined
        ? `<div class="empty">loading events…</div>`
        : events.error
        ? `<div class="empty">Cloud API: ${esc(events.error)}</div>`
        : events.length
        ? `<ol class="evidence run-log">${events
            .map(
              (e) =>
                `<li><span class="muted">${shortClock(e.at)}</span> <span class="tag">${esc(e.type)}</span> ${esc(e.text || "")}</li>`
            )
            .join("")}</ol>`
        : `<div class="empty">No public events.</div>`;
    return (
      main +
      `<tr class="detail"><td colspan="5">
        <h4>${esc(id)} · latest events</h4>
        ${listing}
        <div class="muted" style="margin-top:8px">read-only · fetched server-side · the PAT never reaches this page</div>
      </td></tr>`
    );
  }).join("");
  table.innerHTML = head + body;
}

async function loadCloud() {
  try {
    const res = await fetch("/api/cloud/sessions?limit=12", { cache: "no-store" });
    renderCloud(await res.json());
  } catch (err) {
    renderCloud({ configured: true, error: err.message, sessions: [] });
  }
}

async function loadCloudEvents(id) {
  try {
    const res = await fetch(`/api/cloud/events?id=${encodeURIComponent(id)}&limit=40`, {
      cache: "no-store",
    });
    const body = await res.json();
    cloudEvents.set(id, body.events || { error: body.error || "unavailable" });
  } catch (err) {
    cloudEvents.set(id, { error: err.message });
  }
  if (cloudState) renderCloud(cloudState);
}

async function load() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error("state " + res.status);
    state = await res.json();
    render(state);
  } catch (err) {
    toast("Could not load state: " + err.message, true);
  }
}

async function approve(ticketId, cycleFile, button) {
  if (busy) return;
  const ok = confirm(
    `Approve ${ticketId}?\n\nThis writes a hash-bound approval to runtime/approvals.json.\nIt does not place an order - only the Guard submits on a manual run.`
  );
  if (!ok) return;
  busy = true;
  if (button) button.disabled = true;
  try {
    const res = await fetch("/api/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_id: ticketId, cycle_file: cycleFile }),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) {
      toast(body.error || "approval failed", true);
    } else {
      toast(`Approved ${ticketId} until ${clock(body.expires_at)}`);
      await load();
    }
  } catch (err) {
    toast("approval error: " + err.message, true);
  } finally {
    if (button) button.disabled = false;
    busy = false;
  }
}

document.addEventListener("click", (event) => {
  const approveBtn = event.target.closest("[data-approve]");
  if (approveBtn) {
    event.stopPropagation();
    approve(approveBtn.dataset.approve, approveBtn.dataset.cycle, approveBtn);
    return;
  }
  const cloudRow = event.target.closest("[data-cloud]");
  if (cloudRow) {
    const id = cloudRow.dataset.cloud;
    cloudOpen = cloudOpen === id ? null : id;
    if (cloudOpen && !cloudEvents.has(id)) {
      cloudEvents.set(id, undefined);
      renderCloud(cloudState);
      loadCloudEvents(id);
      return;
    }
    renderCloud(cloudState);
    return;
  }
  const row = event.target.closest("[data-row]");
  if (row) {
    openDetail = openDetail === row.dataset.row ? null : row.dataset.row;
    renderDecisions(state);
  }
});

document.getElementById("refresh").addEventListener("click", () => {
  load();
  loadCloud();
});
load();
loadCloud();
setInterval(() => {
  load();
  loadCloud();
}, 30000);
