(() => {
  if (!HOSTED_SCOUT) return;
  const button = document.getElementById('run-scout');
  const refreshButton = document.getElementById('refresh');
  const statusElement = document.getElementById('action-status');
  const section = document.getElementById('scout-report');
  const content = document.getElementById('scout-report-body');
  const active = new Set(['creating', 'sending', 'running', 'unknown']);
  const local = document.documentElement.dataset.localScout === '1';
  const messages = {
    scout_storage_unavailable: local ? 'The local SQLite database is unavailable. Previous results are retained; no run will be restarted.' : 'Scout is unavailable: private run storage is not ready. The saved dashboard is unchanged.',
    scout_not_configured: local ? 'Local SQLite is ready. Server-side Qoder access is not configured; no credential files were loaded.' : 'Scout is not configured on this site yet.',
    run_active: 'A Scout run is already active. Refresh to follow it.',
    version_conflict: 'The run changed in another tab. Refresh to see the current state.',
    cooldown: 'Please wait five minutes between Scout runs.',
    dispatch_unknown: 'Scout dispatch could not be confirmed. Refresh to inspect this run; no new run will be started.',
    dispatch_rejected: 'The cloud service rejected this Scout request.',
    cloud_unavailable: 'Scout progress is temporarily unavailable. Refresh to check this run without restarting it.',
    invalid_cloud_response: 'The cloud service returned an invalid response. Previous results are retained.',
    response_too_large: 'The Scout result exceeded the size limit. Previous results are retained.',
    invalid_scout_result: 'Scout evidence was incomplete, stale or mismatched. Previous results are retained.',
    scout_failed: 'Scout ended without delivering a result. No orders were placed.',
    scout_timeout: 'Scout has not delivered a confirmed result within 30 minutes. Refresh checks the existing run; it will not restart it.',
    invalid_opening_record: 'Opening record rejected. Use the JSON template, paper account 5829804, market 100, USD, a positive equity value, and the matching trading date at 09:30 ET. Source reference and evidence are required; future or midday balances are not accepted.',
    opening_record_conflict: 'A different opening record already exists for that account and date. It was not replaced. Refresh to inspect the saved evidence.',
    ticket_generation_unavailable: 'Local ticket checks could not finish. Check Python and project dependencies, then Refresh to retry this result without starting another Scout. Previous results are retained.',
  };
  const approvalReasons = {
    report_not_current: 'Only the latest completed Scout result can be approved.',
    ticket_not_eligible: 'Approval blocked: resolve all gate failures other than R11 first.',
    historical_assessment: 'Historical assessment: run Scout for current tickets before approving.',
    ticket_review_expired: 'The ticket review window expired. Run Scout for current tickets.',
    approval_expired: 'Approval expired. Run Scout for current tickets; no renewal is automatic.',
    already_approved: 'Approval recorded; no execution.',
    ticket_changed: 'The ticket or proposal hash changed. Refresh and review it again.',
  };
  Object.assign(messages, approvalReasons);
  const selected = new Set();
  let current = null, working = false, error = '', notice = '', timer = null, lastView = null;

  async function requestJson(action, body) {
    const init = {cache: 'no-store', credentials: 'same-origin', headers: {'Accept': 'application/json'}, signal: AbortSignal.timeout(65000)};
    if (body) Object.assign(init, {method: 'POST', headers: {...init.headers, 'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let res;
    try { res = await fetch(`/functions/v1/app?action=${action}`, init); }
    catch { throw new Error('The request could not be confirmed. Refresh to inspect the existing run before doing anything else.'); }
    if (res.status === 401 || res.status === 403) throw new Error(res.status === 401 ? 'Sign in to this private dashboard to use Scout.' : 'You do not have access to this action.');
    if (res.redirected || !res.headers.get('content-type')?.includes('application/json')) throw new Error('The Scout service returned an unexpected response. Check your sign-in and site setup.');
    let value;
    try { value = await res.json(); } catch { throw new Error('The Scout service returned invalid JSON.'); }
    const code = typeof value?.error === 'string' ? value.error : (!res.ok || value?.ok === false) && typeof value?.code === 'string' ? value.code : null;
    if (!res.ok || value?.ok === false || code) throw new Error(Object.hasOwn(messages, code) ? messages[code] : 'The Scout request failed. Refresh to check the existing run.');
    if (!value || value.ok !== true || !['idle', 'creating', 'sending', 'running', 'unknown', 'completed', 'failed'].includes(value.status) || !Number.isSafeInteger(value.version) || typeof value.configured !== 'boolean' || value.trading_enabled !== false) throw new Error('The Scout service returned an invalid result.');
    if (value.report && (!Array.isArray(value.report.symbols) || !Array.isArray(value.report.omitted_symbols) || !value.report.account || !Array.isArray(value.report.account.positions) || !Array.isArray(value.report.account.open_orders) || !Array.isArray(value.report.account.filled_orders))) throw new Error('The Scout report was incomplete.');
    return value;
  }

  function orders(rows) {
    if (!rows.length) return '<p class="muted">None reported.</p>';
    return `<div class="table-wrap"><table><tr><th>Order</th><th>Symbol</th><th>Side</th><th>Filled / quantity</th><th>Average fill</th><th>Status</th></tr>${rows.map(row => `<tr><td>${esc(row.order_id)}</td><td>${esc(row.symbol)}</td><td>${esc(row.side)}</td><td>${esc(row.filled_quantity)} / ${esc(row.quantity)}</td><td>${usd(row.average_fill_price)}</td><td>${esc(row.status)}</td></tr>`).join('')}</table></div>`;
  }

  function approvalControl(row) {
    if (!local || !current?.approvals?.enabled) return '';
    const item = current.approvals.tickets.find(item => item.ticket_id === row.ticket_id);
    if (!item) return '';
    const saved = item.approval;
    return `<div class="scout-approval"><label><input type="checkbox" data-scout-ticket="${esc(row.ticket_id)}" ${item.eligible ? '' : 'disabled'}> Select ${esc(row.ticket_id)}</label>
      ${saved ? `<p><span class="pill ${item.status === 'APPROVED' ? 'approved' : 'expired'}">${item.status === 'APPROVED' ? 'APPROVAL RECORDED' : 'APPROVAL EXPIRED'}</span> · No execution<br>Approved ${esc(clock(saved.approved_at))} · Expires ${esc(clock(saved.expires_at))}</p>` : ''}
      ${item.reason && item.reason !== 'already_approved' ? `<p class="muted">${esc(approvalReasons[item.reason] || 'Approval unavailable.')}</p>` : ''}</div>`;
  }

  function ticketReview(value) {
    const cycle = value.cycle;
    if (!cycle) return local ? '<p class="banner">This saved report has no ticket checks. Click Refresh to retrieve the same result and generate a read-only assessment; no new Scout or orders will be started.</p>' : '';
    const rejected = cycle.records.filter(row => row.status === 'REJECTED').length;
    const enabled = local && current?.approvals?.enabled;
    const approved = enabled ? current.approvals.tickets.filter(item => item.status === 'APPROVED').length : 0;
    const awaiting = cycle.records.filter(row => row.status === 'AWAITING_APPROVAL').length - approved;
    const rows = cycle.records.map(row => `<tr><td>${esc(row.ticket_id)}</td><td class="sym">${esc(row.symbol)}</td><td>${esc(row.side)}</td><td class="num">${esc(row.quantity)}</td><td class="num">${usd(row.limit_price)}</td><td class="num">${usd(row.est_cost)}</td><td><span class="pill ${pillClass(row.status)}">${esc(row.status)}</span></td></tr><tr><td colspan="7" class="scout-ticket-reasons">${approvalControl(row)}<div class="scout-rejection-list">${(row.gate.rejections || []).map(item => `<p><strong>${esc(item.code)}</strong><br>${esc(item.message)}</p>`).join('')}</div><details><summary>Rationale and evidence</summary><p>${esc(row.rationale)}</p><p>Checked: ${esc(row.gate.checked.join(', '))}</p><p>Proposal hash: ${esc(row.proposal_hash)}</p>${row.evidence.map(item => `<p>${esc(item.kind)} · ${esc(item.source)} · ${esc(clock(item.as_of))}</p>`).join('')}</details></td></tr>`).join('');
    return `<section aria-labelledby="scout-decisions-heading" class="scout-decisions">
      <h3 id="scout-decisions-heading">Scout ticket decisions · execution disabled</h3>
      <p class="muted">${cycle.records.length} ticket${cycle.records.length === 1 ? '' : 's'} · ${rejected} rejected · ${awaiting} awaiting approval${enabled ? ` · ${approved} approvals recorded` : ' (disabled)'} · ${cycle.skipped.length} skipped. ${enabled ? 'Approval recording is enabled; trading is disabled' : 'Approvals and execution are disabled'}; the Guard halt is unchanged.</p>
      ${enabled ? `<p class="muted">Select eligible tickets, then approve the selection. Approvals are hash-bound, expire after ${esc(current.approvals.ttl_minutes / 60)} hours, and stay in SQLite—not the Guard queue. Gate results below are the original assessment, not current execution eligibility.</p><div class="scout-approval-toolbar"><button id="scout-approve" class="primary" disabled>Approve selected (0)</button><span class="muted">Records approval only. No orders or Guard calls.</span></div>` : ''}
      <p class="muted">Evidence assessed as of ${esc(clock(cycle.generated_at))}. ${cycle.historical ? 'Historical assessment using current rules and the original evidence timestamps; not current trading eligibility.' : 'This assessment is saved with this Scout run; refresh does not renew evidence or approve tickets.'}</p>
      ${cycle.baseline_verified === false ? '<p class="banner bad">R8: Verified day-opening equity is unavailable. A current account balance is not an opening baseline.</p>' : ''}
      ${cycle.baseline ? `<p class="banner">R8 assessed against ${usd(Number(cycle.baseline.opening_equity))} opening equity for ${esc(cycle.baseline.trading_day)} · Equity change ${(cycle.daily_pnl_pct * 100).toFixed(2)}%. The configured daily-stop rule and all other gates still apply.</p><p class="muted scout-hash">Operator-attested source: ${esc(cycle.baseline.source_reference)}<br><span class="scout-hash">Import SHA-256: ${esc(cycle.baseline.sha256)}</span></p>` : ''}
      ${rows ? `<div class="table-wrap"><table id="scout-decisions"><thead><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Limit</th><th class="num">Est cost</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="empty">No ticket candidates were generated. See the strategy skip reasons below.</p>'}
      ${cycle.skipped.length ? `<details class="scout-skips"><summary>Strategy skip reasons (${cycle.skipped.length})</summary><div class="table-wrap"><table><tr><th>Symbol</th><th>Reason</th></tr>${cycle.skipped.map(row => `<tr><td>${esc(row.symbol)}</td><td>${esc(row.reason)}</td></tr>`).join('')}</table></div></details>` : ''}
    </section>`;
  }

  function openingPanel() {
    if (!local || !current?.baselines?.enabled) return '';
    const baseline = current.baselines.current;
    return `<section class="scout-opening" aria-labelledby="scout-opening-heading"><h3 id="scout-opening-heading">Opening-equity evidence · paper account 5829804</h3>
      <p>${baseline ? `<strong>${usd(Number(baseline.opening_equity))}</strong> · ${esc(baseline.trading_day)} · Operator-attested broker opening record` : `No opening record for ${esc(current.baselines.current_day)} (ET). R8 stays blocked without matching evidence.`}</p>
      ${baseline ? `<p class="muted">Effective ${esc(clock(baseline.as_of))} · Imported ${esc(clock(baseline.imported_at))}<br>Source: ${esc(baseline.source_reference)}<br><span class="scout-hash">Import SHA-256: ${esc(baseline.sha256)}</span></p>` : ''}
      ${current.baselines.reassessment_available ? '<p class="banner">Matching evidence is available for the saved report. Click Refresh to re-evaluate R8 at its original timestamps. This remains a historical assessment; run a new Scout for current approvable tickets.</p>' : ''}
      <details><summary>Import a verified opening record</summary>
        <p>This is your attestation, not independent broker verification. Use a broker opening snapshot or dated statement that establishes equity at the regular-session 09:30 ET open. Do not substitute a current balance or reconstruct a value from prior closes.</p>
        <p class="muted">Download the blank JSON template, fill it from that evidence, and select only that file (UTF-8, up to 16 KiB). Set as_of to the effective opening time with its UTC offset, not the download time. source_reference identifies the broker record; source_evidence contains its relevant opening-equity excerpt. Keep unrelated personal details out.</p>
        <p class="muted">The imported JSON is retained verbatim and hashed in local SQLite. Its hash identifies this import, not a separately held PDF or broker signature. One immutable record per account and ET trading date; it is never reused on another date. Importing does not approve tickets or call the Guard.</p>
        <div class="scout-approval-toolbar"><button id="scout-opening-template">Download blank template</button><label>Opening record <input id="scout-opening-file" type="file" accept=".json,application/json"></label><button id="scout-opening-import" disabled>Review and import</button></div>
      </details></section>`;
  }

  function downloadOpeningTemplate() {
    const source = {version: 1, kind: 'broker_opening_equity', account_id: '5829804', market: 100, currency: 'USD',
      trading_day: current.baselines.current_day, as_of: '', opening_equity: '', source_reference: '', source_evidence: ''};
    const url = URL.createObjectURL(new Blob([JSON.stringify(source, null, 2) + '\n'], {type: 'application/json'}));
    const link = document.createElement('a');
    link.href = url; link.download = 'opening-record-template.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function importOpening() {
    if (working || error || !local || !current?.baselines?.enabled) return;
    const file = document.getElementById('scout-opening-file')?.files[0];
    if (!file) return;
    clearTimeout(timer); working = true; notice = ''; render();
    try {
      if (file.size > 16384) throw new Error('Select a UTF-8 JSON opening record no larger than 16 KiB.');
      let source_text, record;
      try { source_text = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(await file.arrayBuffer()); record = JSON.parse(source_text); }
      catch { throw new Error('The selected file is not valid UTF-8 JSON. Use the blank opening-record template.'); }
      if (!record || Array.isArray(record) || typeof record !== 'object') throw new Error(messages.invalid_opening_record);
      if (!window.confirm(`Import opening equity ${String(record.opening_equity).slice(0, 40)} ${String(record.currency).slice(0, 10)} for account ${String(record.account_id).slice(0, 40)}?\nDate: ${String(record.trading_day).slice(0, 30)}\nEffective: ${String(record.as_of).slice(0, 50)}\nSource: ${String(record.source_reference).slice(0, 300)}\n\nI verified this value against the broker opening record, not a current balance or prior-close estimate. This immutable import is my attestation, not independent verification. Trading stays disabled and the Guard halt stays in place.`)) return;
      current = await requestJson('scout-import-opening', {source_text, confirmed: true});
      notice = `Opening record for ${record.trading_day} saved. Refresh rechecks matching saved evidence; Run Scout retrieves a new assessment. No tickets were approved.`;
    } catch (err) { error = err.message; }
    finally { working = false; render(); schedule(); }
  }

  function report(value) {
    if (!value) {
      content.innerHTML = openingPanel() + '<p class="empty">Run Scout to retrieve paper-account facts, trend measurements and dated news. Results appear here automatically.</p><p class="muted">No orders or Guard execution. Ticket approvals, when available, require an explicit selection.</p>';
      return;
    }
    const account = value.account;
    document.getElementById('scout-report-time').textContent = clock(value.as_of);
    const rows = value.symbols.map(row => `<tr><td class="sym">${esc(row.symbol)}</td><td class="num">${usd(row.last_price)}</td><td class="num">${money(row.ema8)}</td><td class="num">${money(row.ema21)}</td><td><span class="pill ${row.trend_up ? 'approved' : ''}">${row.trend_up ? 'EMA8 > EMA21' : 'No upward cross'}</span></td><td class="scout-news">${row.news.length ? row.news.map(item => `<p>${esc(item.title)}<br><span class="muted">${esc(clock(item.as_of))} · ${esc(item.source)}</span></p>`).join('') : '<span class="muted">No dated news supplied</span>'}</td></tr>`).join('');
    content.innerHTML = openingPanel() + `<p class="muted">${value.ticket_checks === 'completed' ? 'R1–R11 assessment saved · trading remains disabled · no orders or Guard execution.' : 'Read-only research · not an approved ticket · R1–R11 ticket checks have not run · trading remains disabled.'}</p>
      ${ticketReview(value)}
      <div class="scout-summary"><span>Equity <strong>${usd(account.equity)}</strong></span><span>Cash <strong>${usd(account.cash)}</strong></span><span>${account.positions.length} positions</span><span>${account.open_orders.length} open orders</span></div>
      ${rows ? `<div class="table-wrap"><table><tr><th>Symbol</th><th class="num">Last close</th><th class="num">EMA8</th><th class="num">EMA21</th><th>Trend</th><th>News evidence</th></tr>${rows}</table></div>` : '<p class="empty">No symbol research was delivered. Scout skips research when the regular US session is closed; an empty result alone does not verify market status.</p>'}
      <p class="muted">Not researched: ${esc(value.omitted_symbols.join(', ') || 'none')}.</p>
      <details><summary>Account positions (${account.positions.length})</summary>${account.positions.length ? `<div class="table-wrap"><table><tr><th>Symbol</th><th>Quantity</th><th>Average cost</th><th>Market value</th></tr>${account.positions.map(row => `<tr><td>${esc(row.symbol)}</td><td>${esc(row.quantity)}</td><td>${usd(row.average_cost)}</td><td>${usd(row.market_value)}</td></tr>`).join('')}</table></div>` : '<p class="muted">No positions reported.</p>'}</details>
      <details><summary>Open orders (${account.open_orders.length})</summary>${orders(account.open_orders)}</details>
      <details><summary>Latest filled orders (${account.filled_orders.length}, up to 100)</summary>${orders(account.filled_orders)}</details>`;
  }

  function render() {
    section.hidden = false;
    if (local && current?.approvals?.enabled) document.querySelector('footer').textContent = 'Run Scout retrieves research and paper-account facts on demand. Approve selected records hash-bound approvals in local SQLite only. Trading and Guard execution remain disabled; the Guard halt is unchanged. Refresh checks the current run without starting another Scout.';
    const cooldown = current?.next_run_at && Date.now() < Date.parse(current.next_run_at);
    button.disabled = working || !!error || !current?.configured || active.has(current?.status) || !!cooldown;
    button.textContent = ['creating', 'sending', 'running'].includes(current?.status) ? 'Scout running…' : 'Run Scout';
    refreshButton.disabled = working;
    const fixture = current?.fixture ? 'SYNTHETIC PREVIEW · NO BROKER · ' : local ? 'LOCAL · SQLITE · ' : '';
    const label = current?.error_code && Object.hasOwn(messages, current.error_code) ? messages[current.error_code] :
      current && !current.configured ? messages.scout_not_configured :
      current?.status === 'completed' ? `Scout complete · ${clock(current.report?.as_of)}${cooldown ? ` · next run after ${clock(current.next_run_at)}` : ''}` :
      active.has(current?.status) ? `Scout ${current.status} · results appear automatically · started ${clock(current.started_at)}` :
      current ? 'Run Scout for fresh research. Refresh checks this run only. No orders will be placed.' : 'Checking Scout availability…';
    statusElement.textContent = fixture + (error || notice || label);
    if (current !== lastView || !content.innerHTML) { selected.clear(); lastView = current; report(current?.report ?? null); }
    renderSelection();
  }

  function renderSelection() {
    for (const input of content.querySelectorAll('[data-scout-ticket]')) {
      const item = current?.approvals?.tickets.find(item => item.ticket_id === input.dataset.scoutTicket);
      input.disabled = working || !!error || !item?.eligible;
      input.checked = selected.has(input.dataset.scoutTicket);
    }
    const openingFile = document.getElementById('scout-opening-file');
    if (openingFile) openingFile.disabled = working || !!error;
    const importButton = document.getElementById('scout-opening-import');
    if (importButton) importButton.disabled = working || !!error || !openingFile?.files.length;
    const approveButton = document.getElementById('scout-approve');
    if (approveButton) {
      approveButton.disabled = working || !!error || !selected.size;
      approveButton.textContent = `Approve selected (${selected.size})`;
    }
  }

  async function approveSelected() {
    if (working || error || !local || !current?.approvals?.enabled || !selected.size) return;
    const tickets = current.approvals.tickets.filter(item => selected.has(item.ticket_id) && item.eligible)
      .map(({ticket_id, proposal_hash}) => ({ticket_id, proposal_hash}));
    if (!tickets.length || !window.confirm(`Record approval for ${tickets.length} selected ticket(s)?\n\nTrading stays disabled and the Guard halt stays in place. No orders will be sent.`)) return;
    notice = '';
    clearTimeout(timer);
    working = true;
    render();
    try { current = await requestJson('scout-approve', {request_id: current.request_id, version: current.version, tickets}); error = ''; }
    catch (err) { error = err.message; selected.clear(); }
    finally { working = false; render(); schedule(); }
  }

  content.addEventListener('change', event => {
    const input = event.target;
    if (input.id === 'scout-opening-file') { renderSelection(); return; }
    if (!input.matches('[data-scout-ticket]') || input.disabled) return;
    if (input.checked) selected.add(input.dataset.scoutTicket); else selected.delete(input.dataset.scoutTicket);
    renderSelection();
  });
  content.addEventListener('click', event => {
    if (event.target.closest('#scout-approve')) approveSelected();
    if (event.target.closest('#scout-opening-import')) importOpening();
    if (event.target.closest('#scout-opening-template')) downloadOpeningTemplate();
  });

  function schedule() {
    clearTimeout(timer);
    if (!error && ['creating', 'sending', 'running'].includes(current?.status)) timer = setTimeout(sync, 5000);
    else if (!error && current?.next_run_at && Date.now() < Date.parse(current.next_run_at)) timer = setTimeout(render, Math.min(300000, Date.parse(current.next_run_at) - Date.now() + 50));
  }

  async function sync() {
    if (working || !current?.request_id) return;
    notice = '';
    working = true;
    render();
    try { current = await requestJson('scout-sync', {request_id: current.request_id}); error = ''; }
    catch (err) { error = err.message; }
    finally { working = false; render(); schedule(); }
  }

  async function refresh(reconcileCompleted = true) {
    if (working) return;
    clearTimeout(timer);
    working = true;
    render();
    try { current = await requestJson('scout-status'); error = ''; notice = ''; }
    catch (err) { error = err.message; }
    finally { working = false; render(); }
    if (!error && (active.has(current?.status) || (reconcileCompleted && local && current?.status === 'completed' && (current.report?.ticket_checks === 'not_run' || current.baselines?.reassessment_available)))) await sync();
    else schedule();
  }

  async function start() {
    if (button.disabled || !current) return;
    clearTimeout(timer);
    working = true;
    error = ''; notice = '';
    render();
    try {
      current = await requestJson('scout-start', {request_id: crypto.randomUUID().replaceAll('-', ''), version: current.version});
    } catch (err) { error = err.message; }
    finally { working = false; render(); schedule(); }
  }

  window.hostedScout = {start, refresh};
  if (local) document.getElementById('decisions').closest('section').querySelector('h2').textContent = 'Saved decision log · at export';
  render();
  refresh(false);
})();
