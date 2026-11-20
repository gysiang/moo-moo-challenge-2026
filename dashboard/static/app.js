const money = (v) =>
  v === null || v === undefined ? "—" : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usd = (v) => (v === null || v === undefined ? "—" : "$" + money(v));
const pct = (v, digits = 2) => (v === null || v === undefined ? "—" : (v * 100).toFixed(digits) + "%");
const signed = (v) => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + v.toFixed(2));
const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ET_ZONE = "America/New_York";
const etDate = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
};
const clock = (iso) => {
  const d = etDate(iso);
  return d
    ? d.toLocaleString("en-US", {
        timeZone: ET_ZONE,
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
        hour12: true,
      }) + " ET"
    : "—";
};
const shortClock = (iso) => {
  const d = etDate(iso);
  return d
    ? d.toLocaleString("en-US", {
        timeZone: ET_ZONE,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "—";
};
const etTime = (iso) => {
  const d = etDate(iso);
  return d
    ? d.toLocaleTimeString("en-GB", { timeZone: ET_ZONE, hour: "2-digit", minute: "2-digit", hour12: false })
    : "—";
};
const etDay = (iso) => {
  const d = etDate(iso);
  return d
    ? d.toLocaleDateString("en-US", { timeZone: ET_ZONE, weekday: "short", month: "short", day: "numeric" })
    : "—";
};

const selectedTickets = new Map();
let state = null;

function approvalBlock(s) {
  if (STATIC_MODE || s.demo) return "Read-only / demo: no orders";
  if (s.guard?.halted || s.guard?.kill_switch) return "Guard halted — reconcile and explicitly clear before trading";
  if (!s.guard_link?.configured) return "Guard is not configured";
  if (!s.batch?.protocol_ready) return "Cloud Guard batch setup is not confirmed — execution disabled";
  if (!s.baseline_verified) return "Day-opening equity unverified — trading blocked";
  if (s.market?.state !== "OPEN") return "US regular market is closed";
  if (!["idle", "completed"].includes(s.batch?.status || "idle")) return "Prior batch unresolved — inspect Guard";
  if (s.batch?.completed_at && new Date(s.account_as_of) <= new Date(s.batch.completed_at)) return "Refresh broker state after the last batch";
  return "";
}

function eligible(d, s) {
  const age = (Date.now() - new Date(d.generated_at).getTime()) / 60000;
  const accountAge = (Date.now() - new Date(s.account_as_of).getTime()) / 60000;
  return !approvalBlock(s) && d.status === "AWAITING_APPROVAL" && !d.guard_state && d.proposal_hash &&
    (!d.request_id || d.request_id === s.scout_request_id) && age >= -5 && age <= s.limits.guard_max_snapshot_age_minutes && accountAge >= -5 &&
    accountAge <= s.limits.guard_max_snapshot_age_minutes &&
    (d.side !== "BUY" || new Date(s.market.closes_at).getTime() - Date.now() > 30 * 60000);
}

function renderControls(s) {
  const action = s.manual_action || {};
  const active = ["creating", "sending", "running", "unknown"].includes(action.status);
  const configured = action.configured && !STATIC_MODE && !s.demo;
  if (!HOSTED_SCOUT) {
  document.getElementById("run-scout").disabled = busy || active || !configured;
  document.getElementById("refresh").disabled = busy || (!STATIC_MODE && (active || !configured));
  const label = action.mode === "scout" ? "Scout" : "Account refresh";
  document.getElementById("action-status").textContent = action.error ||
    (action.status === "completed" ? `${label} complete · account as of ${clock(s.account_as_of)}` :
      active ? `${label} ${action.status} · results appear automatically · ${action.session_id || "creating session"}` :
      configured ? "On demand only · Run Scout for tickets · Refresh for positions and orders" : "Cloud actions disabled (demo/offline)");
  }
  for (const [key, choice] of selectedTickets) {
    if (!(s.decisions || []).some(d => `${d.cycle}|${d.ticket_id}` === key && d.proposal_hash === choice.proposal_hash && eligible(d, s))) selectedTickets.delete(key);
  }
  const block = approvalBlock(s);
  document.getElementById("approval-note").textContent = block || "Fresh tickets only · combined limits checked · one reconciled order at a time";
  document.getElementById("select-tickets").disabled = busy || !!block;
  const button = document.getElementById("approve-selected");
  button.disabled = busy || !!block || selectedTickets.size === 0;
  button.textContent = `Approve selected (${selectedTickets.size})`;
}
let openDetail = null;
let busy = false;
let cloudState = null;
let cloudOpen = null;
const cloudEvents = new Map();
const STATIC_MODE = document.documentElement.dataset.static === "1";
const HOSTED_SCOUT = STATIC_MODE && document.documentElement.dataset.hostedScout === "1";
const STATE_URL = STATIC_MODE ? "state.json" : "/api/state";
const CLOUD_URL = STATIC_MODE ? "cloud.json" : "/api/cloud/sessions?limit=12";

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

function renderFills(s) {
  const rows = s.filled_orders || [];
  document.getElementById("fills-count").textContent = `${rows.length} orders with fills · as of ${clock(s.account_as_of)}`;
  document.getElementById("fills").innerHTML = `<tr><th>Order</th><th>Symbol</th><th>Side</th><th class="num">Filled / ordered</th><th class="num">Avg fill</th><th>Status</th></tr>` +
    (rows.length ? rows.map(o => `<tr><td>${esc(o.order_id)}</td><td class="sym">${esc(o.symbol)}</td><td>${esc(o.side)}</td><td class="num">${esc(o.filled_quantity)} / ${esc(o.quantity)}</td><td class="num">${money(o.average_fill_price)}</td><td>${esc(o.status)}</td></tr>`).join("") : `<tr><td colspan="6" class="empty">No retrieved fills. Refresh to fetch broker history.</td></tr>`);
}

function renderGuard(s) {
  const g = s.guard || {};
  const banner = g.halted || g.kill_switch
    ? `<div class="banner bad">order flow latched — ${g.kill_switch ? "kill switch" : "halt"}${g.halted && g.kill_switch ? " + halt" : ""}</div>`
    : `<div class="banner ok">order flow normal</div>`;
  const m = s.exit_mandate;
  const mandate = !m
    ? `<div class="line"><span>exit mandate</span><span>none — exits need approval</span></div>`
    : m.error
    ? `<div class="banner bad">exit mandate unreadable — ${esc(m.error)}</div>`
    : `<div class="line"><span>exit mandate</span><span>${m.active ? "active" : "expired"} · ${esc(m.mandate_id)}</span></div>
    <div class="line"><span>mandate expires</span><span>${clock(m.expires_at)}</span></div>
    <div class="line"><span>max exits / run</span><span>${m.max_exits_per_run}</span></div>`;
  document.getElementById("guard").innerHTML = `
    ${banner}
    <div class="line"><span>events</span><span>${g.events ?? 0}</span></div>
    <div class="line"><span>halted</span><span>${g.halted ? "yes" : "no"}</span></div>
    <div class="line"><span>kill switch</span><span>${g.kill_switch ? "yes" : "no"}</span></div>
    <div class="line"><span>approvals valid</span><span>${Object.keys(s.approvals || {}).length}</span></div>
    <div class="line"><span>operator rejects</span><span>${(s.counts || {}).operator_rejected ?? 0}</span></div>
    ${mandate}
    <div class="line"><span>execution</span><span>${esc(approvalBlock(s) || "per-selection cloud batch")}</span></div>
    <div class="line"><span>latest batch</span><span>${esc(s.batch?.status || "none")}</span></div>
    ${s.batch?.error ? `<div class="banner bad">${esc(s.batch.error)}</div>` : ""}
    ${(s.batch?.outcomes || []).map(o => `<div class="line"><span>${esc(o.ticket_id)}</span><span>${esc(o.status)}${o.order_id ? ` · ${esc(o.order_id)}` : ""}</span></div>`).join("")}`;
}

const pillClass = (status) =>
  status === "READY" ? "ready"
  : status === "AWAITING_APPROVAL" ? "awaiting"
  : status === "APPROVED" ? "approved"
  : status === "APPROVAL_EXPIRED" ? "expired"
  : status === "REJECTED" || status === "REJECTED_BY_OPERATOR" ? "rejected"
  : "";

function renderDecisions(s) {
  const rows = s.decisions || [];
  const c = s.counts || {};
  const guardOn = (s.guard_link || {}).configured;
  const mandate = s.exit_mandate || null;
  const mandateOn = Boolean(mandate && mandate.active && !mandate.error);
  document.getElementById("decision-counts").innerHTML = `
    <span>awaiting ${c.awaiting ?? 0}</span><span>approved ${c.approved ?? 0}</span>
    <span>rejected ${c.rejected ?? 0}</span><span>operator rejected ${c.operator_rejected ?? 0}</span>
    <span>ready ${c.ready ?? 0}</span>`;
  const head = `<tr><th class="col-time">Generated</th><th>Ticket</th><th>Side</th><th>Symbol</th><th class="num">Qty</th><th class="num">Limit</th><th class="num col-cost">Est cost</th><th>Gate</th><th>Status</th><th></th></tr>`;
  if (!rows.length) {
    document.getElementById("decisions").innerHTML = head + `<tr><td colspan="10" class="empty">No cycle reports in <code>runtime/</code> yet.</td></tr>`;
    return;
  }
  const body = rows.map((d) => {
    const key = `${d.cycle}|${d.ticket_id}`;
    const codes = (d.gate_codes || []).join(", ") || "ok";
    const short = (d.gate_codes || []).map((c) => String(c).split("_")[0]).join(", ") || "ok";
    const canApprove = eligible(d, s);
    const canReject = !STATIC_MODE && d.status === "AWAITING_APPROVAL";
    const action = [
      canApprove
        ? `<button class="primary" data-approve="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Approve${guardOn ? " + Guard" : ""}</button>`
        : "",
      canReject
        ? `<button class="ghost" data-reject="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Reject</button>`
        : "",
    ].filter(Boolean).join(" ");
    const main = `<tr class="row-toggle" data-row="${esc(key)}">
      <td class="col-time">${clock(d.generated_at)}</td>
      <td>${canApprove ? `<input type="checkbox" data-select="${esc(key)}" aria-label="Select ${esc(d.ticket_id)}" ${selectedTickets.has(key) ? "checked" : ""}> ` : ""}${esc(d.ticket_id)}</td>
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
          ${d.exit_reason ? `<h4>Exit reason</h4><p>${esc(d.exit_reason)}${mandateOn && d.side === "SELL" ? " · covered by the operator exit mandate" : ""}</p>` : ""}
          <h4>Cycle</h4>
          <p>${esc(d.cycle)} · slot ${esc(d.slot || "—")} · ${esc(d.sleeve || "")}</p>
          <h4>Generated / gate</h4>
          <p>${clock(d.generated_at)} · ${esc(codes)}</p>
          ${d.approval_expires_at ? `<h4>Approval expires</h4><p>${clock(d.approval_expires_at)}</p>` : ""}
          ${d.guard_triggered_at ? `<h4>Guard run</h4><p>${clock(d.guard_triggered_at)} · ${esc(d.guard_session_id || "")}</p>` : ""}
          ${d.guard_state && d.guard_state !== "accepted" ? `<h4>Guard trigger</h4><p>${esc(d.guard_state)}${d.guard_state === "unknown" ? " · check the Guard session before retrying" : ""}</p>` : ""}
          ${d.rejected_at ? `<h4>Rejected by operator</h4><p>${clock(d.rejected_at)} · ${esc(d.rejection_reason || "")}</p>` : ""}
        </div>
        <div>
          <h4>Evidence</h4>
          <ul class="evidence">${evidence || "<li>none</li>"}</ul>
          ${canApprove ? `<div class="approve-box">
            <button class="primary" data-approve="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Approve this ticket</button>
            <span class="muted">Hash-bound approval · Guard re-checks every rule</span>
          </div>` : ""}
          ${canReject ? `<div class="approve-box">
            <button class="ghost" data-reject="${esc(d.ticket_id)}" data-cycle="${esc(d.cycle)}">Reject this ticket</button>
            <span class="muted">appends to rejections.jsonl · nothing is sent</span>
          </div>` : ""}
        </div>
      </div>
    </td></tr>`;
    return main + detail;
  }).join("");
  document.getElementById("decisions").innerHTML = head + body;
}

function renderSession(s) {
  const m = s.market || {};
  const el = document.getElementById("session");
  const open = m.state === "OPEN";
  el.dataset.state = m.state ? m.state.toLowerCase() : "unknown";
  document.getElementById("session-state").textContent = m.state
    ? open ? "US market open" : "US market closed"
    : "US market";
  const hours = m.session_open && m.session_close ? `${m.session_open}–${m.session_close} ET` : "";
  let detail = "session calendar unavailable";
  if (open) {
    detail = `${m.reason === "early close" ? "early close" : "regular session"} · closes ${etTime(m.closes_at)}`;
  } else if (m.state) {
    const opens = m.next_open ? `opens ${etDay(m.next_open)} ${etTime(m.next_open)}` : "no open session scheduled";
    detail = `${m.reason || "closed"} · ${opens}`;
  }
  if (STATIC_MODE && m.state) detail += " · at export";
  document.getElementById("session-detail").textContent = detail;
  el.title = hours
    ? `US regular session ${hours}${m.early_close ? " (early close)" : ""} · ${m.reason || ""}`
    : "US regular session 09:30–16:00 ET";
}

function render(s) {
  document.getElementById("account-id").textContent = s.account_id || "—";
  document.getElementById("mode").innerHTML = HOSTED_SCOUT
    ? `PAPER · <span class="demo-badge">${s.demo ? 'SYNTHETIC PREVIEW · ' : ''}MANUAL SCOUT · TRADING DISABLED</span>`
    : STATIC_MODE
    ? `PAPER · <span class="demo-badge">READ-ONLY SNAPSHOT</span>`
    : s.verification_fixture || s.demo
    ? `DEMO <span class="demo-badge">SYNTHETIC DATA · NO BROKER</span>`
    : "PAPER";
  document.getElementById("as-of").textContent = clock(s.account_as_of || s.as_of);
  renderControls(s);
  renderSession(s);
  renderKpis(s);
  renderChart(s);
  renderPositions(s);
  renderOrders(s);
  renderFills(s);
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
    table.innerHTML = head + `<tr><td colspan="5" class="empty">${
      STATIC_MODE
        ? "Cloud session reads are not included in the read-only snapshot."
        : "Cloud session reads are off — no cloud token is configured on the local server."
    }</td></tr>`;
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
      <td class="col-time" title="${esc(clock(s.created_at))}">${shortClock(s.created_at)}</td>
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
                `<li><span class="muted" title="${esc(clock(e.at))}">${shortClock(e.at)}</span> <span class="tag">${esc(e.type)}</span> ${esc(e.text || "")}</li>`
            )
            .join("")}</ol>`
        : `<div class="empty">No public events.</div>`;
    return (
      main +
      `<tr class="detail"><td colspan="5">
        <h4>${esc(id)} · latest events</h4>
        ${listing}
        <div class="muted" style="margin-top:8px">${
          STATIC_MODE ? "read-only snapshot · captured at export time" : "read-only · fetched server-side · the PAT never reaches this page"
        }</div>
      </td></tr>`
    );
  }).join("");
  table.innerHTML = head + body;
}

async function loadCloud() {
  try {
    const res = await fetch(CLOUD_URL, { cache: "no-store" });
    renderCloud(await res.json());
  } catch (err) {
    renderCloud({ configured: true, error: err.message, sessions: [] });
  }
}

async function loadCloudEvents(id) {
  if (STATIC_MODE) {
    cloudEvents.set(id, { error: "the event stream is not part of the read-only snapshot" });
    if (cloudState) renderCloud(cloudState);
    return;
  }
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
    const res = await fetch(STATE_URL, { cache: "no-store" });
    if (!res.ok) throw new Error("state " + res.status);
    state = await res.json();
    render(state);
  } catch (err) {
    toast("Could not load state: " + err.message, true);
  }
}

async function approve(ticketId, cycleFile) {
  const row = state?.decisions.find(d => d.ticket_id === ticketId && d.cycle === cycleFile);
  if (row) await approveBatch([{ticket_id: ticketId, cycle_file: cycleFile, proposal_hash: row.proposal_hash}]);
}

async function approveBatch(tickets) {
  if (STATIC_MODE || busy || !tickets.length || approvalBlock(state)) return;
  const rows = tickets.map(t => state.decisions.find(d => d.ticket_id === t.ticket_id && d.cycle === t.cycle_file));
  if (rows.some(d => !d || !eligible(d, state))) return toast("Selection changed or expired. Reload and review.", true);
  const details = rows.map(d => `${d.ticket_id}: ${d.side} ${d.quantity} ${d.symbol} @ ${usd(d.limit_price)}`).join("\n");
  if (!confirm(`Approve these ${tickets.length} paper order(s)?\n\n${details}\n\nGuard re-checks combined limits, then executes serially with reconciliation. No resizing or automatic retries.`)) return;
  busy = true;
  renderControls(state);
  try {
    const res = await fetch("/api/approve-batch", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({tickets})});
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || "Batch was not accepted");
    selectedTickets.clear();
    toast(`Approved ${tickets.length} tickets · Guard batch ${body.status}`);
  } catch (err) {
    toast(`${err.message} · Check batch status before retrying.`, true);
  } finally {
    busy = false;
    await load();
    await loadCloud();
  }
}

async function startAction(mode) {
  if (STATIC_MODE || busy) return;
  busy = true;
  renderControls(state);
  try {
    const res = await fetch(`/api/${mode}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || "Action not accepted");
    toast(`${mode === "scout" ? "Scout" : "Account refresh"} started; results will appear automatically.`);
  } catch (err) {
    toast(`${err.message} · Inspect action status before retrying.`, true);
  } finally {
    busy = false;
    await load();
    await loadCloud();
  }
}

async function reject(ticketId, cycleFile, button) {
  if (STATIC_MODE) {
    toast("Read-only snapshot — rejections run from the local operator dashboard.", true);
    return;
  }
  if (busy) return;
  const ok = confirm(
    `Reject ${ticketId}?\n\nThis appends a rejection record to runtime/rejections.jsonl and blocks approval for this ticket.\nNothing is sent to the broker.`
  );
  if (!ok) return;
  const reason = "operator rejected in dashboard";
  busy = true;
  if (button) button.disabled = true;
  try {
    const res = await fetch("/api/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticket_id: ticketId, cycle_file: cycleFile, reason }),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) {
      toast(body.error || "rejection failed", true);
    } else {
      toast(`Rejected ${ticketId} · logged`);
      await load();
    }
  } catch (err) {
    toast("rejection error: " + err.message, true);
  } finally {
    if (button) button.disabled = false;
    busy = false;
  }
}

document.addEventListener("click", (event) => {
  const checkbox = event.target.closest("[data-select]");
  if (checkbox) {
    const key = checkbox.dataset.select;
    const row = state.decisions.find(d => `${d.cycle}|${d.ticket_id}` === key);
    if (checkbox.checked && row && eligible(row, state)) selectedTickets.set(key, {ticket_id: row.ticket_id, cycle_file: row.cycle, proposal_hash: row.proposal_hash});
    else selectedTickets.delete(key);
    renderControls(state);
    return;
  }
  const approveBtn = event.target.closest("[data-approve]");
  if (approveBtn) {
    event.stopPropagation();
    approve(approveBtn.dataset.approve, approveBtn.dataset.cycle, approveBtn);
    return;
  }
  const rejectBtn = event.target.closest("[data-reject]");
  if (rejectBtn) {
    event.stopPropagation();
    reject(rejectBtn.dataset.reject, rejectBtn.dataset.cycle, rejectBtn);
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

document.getElementById("run-scout").addEventListener("click", () => HOSTED_SCOUT ? window.hostedScout?.start() : startAction("scout"));
document.getElementById("refresh").addEventListener("click", () => HOSTED_SCOUT ? window.hostedScout?.refresh() : STATIC_MODE ? Promise.all([load(), loadCloud()]) : startAction("refresh"));
document.getElementById("approve-selected").addEventListener("click", () => approveBatch([...selectedTickets.values()]));
document.getElementById("select-tickets").addEventListener("click", () => {
  if (STATIC_MODE || !state) return;
  selectedTickets.clear();
  for (const d of state.decisions.filter(d => eligible(d, state)).slice(0, 20)) selectedTickets.set(`${d.cycle}|${d.ticket_id}`, {ticket_id: d.ticket_id, cycle_file: d.cycle, proposal_hash: d.proposal_hash});
  renderControls(state);
  renderDecisions(state);
});
load();
loadCloud();
if (!STATIC_MODE) {
  setInterval(load, 5000);
  setInterval(loadCloud, 30000);
}
