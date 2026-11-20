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
    ticket_generation_unavailable: 'Local ticket checks could not finish. Check Python and project dependencies, then Refresh to retry this result without starting another Scout. Previous results are retained.',
  };
  let current = null, working = false, error = '', timer = null, lastReport = null;

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

  function ticketReview(value) {
    const cycle = value.cycle;
    if (!cycle) return local ? '<p class="banner">This saved report has no ticket checks. Click Refresh to retrieve the same result and generate a read-only assessment; no new Scout or orders will be started.</p>' : '';
    const rejected = cycle.records.filter(row => row.status === 'REJECTED').length;
    const awaiting = cycle.records.filter(row => row.status === 'AWAITING_APPROVAL').length;
    const rows = cycle.records.map(row => `<tr><td>${esc(row.ticket_id)}</td><td class="sym">${esc(row.symbol)}</td><td>${esc(row.side)}</td><td class="num">${esc(row.quantity)}</td><td class="num">${usd(row.limit_price)}</td><td class="num">${usd(row.est_cost)}</td><td><span class="pill ${pillClass(row.status)}">${esc(row.status)}</span></td></tr><tr><td colspan="7" class="scout-ticket-reasons"><div class="scout-rejection-list">${(row.gate.rejections || []).map(item => `<p><strong>${esc(item.code)}</strong><br>${esc(item.message)}</p>`).join('')}</div><details><summary>Rationale and evidence</summary><p>${esc(row.rationale)}</p><p>Checked: ${esc(row.gate.checked.join(', '))}</p><p>Proposal hash: ${esc(row.proposal_hash)}</p>${row.evidence.map(item => `<p>${esc(item.kind)} · ${esc(item.source)} · ${esc(clock(item.as_of))}</p>`).join('')}</details></td></tr>`).join('');
    return `<section aria-labelledby="scout-decisions-heading" class="scout-decisions">
      <h3 id="scout-decisions-heading">Scout ticket decisions · review only</h3>
      <p class="muted">${cycle.records.length} ticket${cycle.records.length === 1 ? '' : 's'} · ${rejected} rejected · ${awaiting} awaiting approval (disabled) · ${cycle.skipped.length} skipped. Approvals and execution are disabled; the Guard halt is unchanged.</p>
      <p class="muted">Evidence assessed as of ${esc(clock(cycle.generated_at))}. ${cycle.historical ? 'Historical assessment using current rules and the original evidence timestamps; not current trading eligibility.' : 'This assessment is saved with this Scout run; refresh does not renew evidence or approve tickets.'}</p>
      ${cycle.baseline_verified === false ? '<p class="banner bad">R8: Verified day-opening equity is unavailable. A current account balance is not an opening baseline.</p>' : ''}
      ${rows ? `<div class="table-wrap"><table id="scout-decisions"><thead><tr><th>Ticket</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Limit</th><th class="num">Est cost</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<p class="empty">No ticket candidates were generated. See the strategy skip reasons below.</p>'}
      ${cycle.skipped.length ? `<details class="scout-skips"><summary>Strategy skip reasons (${cycle.skipped.length})</summary><div class="table-wrap"><table><tr><th>Symbol</th><th>Reason</th></tr>${cycle.skipped.map(row => `<tr><td>${esc(row.symbol)}</td><td>${esc(row.reason)}</td></tr>`).join('')}</table></div></details>` : ''}
    </section>`;
  }

  function report(value) {
    if (!value) {
      content.innerHTML = '<p class="empty">Run Scout to retrieve paper-account facts, trend measurements and dated news. Results appear here automatically.</p><p class="muted">Read-only research only. No approvals, orders or Guard execution.</p>';
      return;
    }
    const account = value.account;
    document.getElementById('scout-report-time').textContent = clock(value.as_of);
    const rows = value.symbols.map(row => `<tr><td class="sym">${esc(row.symbol)}</td><td class="num">${usd(row.last_price)}</td><td class="num">${money(row.ema8)}</td><td class="num">${money(row.ema21)}</td><td><span class="pill ${row.trend_up ? 'approved' : ''}">${row.trend_up ? 'EMA8 > EMA21' : 'No upward cross'}</span></td><td class="scout-news">${row.news.length ? row.news.map(item => `<p>${esc(item.title)}<br><span class="muted">${esc(clock(item.as_of))} · ${esc(item.source)}</span></p>`).join('') : '<span class="muted">No dated news supplied</span>'}</td></tr>`).join('');
    content.innerHTML = `<p class="muted">${value.ticket_checks === 'completed' ? 'R1–R11 evaluated for review only · no approvals or orders · trading remains disabled.' : 'Read-only research · not an approved ticket · R1–R11 ticket checks have not run · trading remains disabled.'}</p>
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
    const cooldown = current?.next_run_at && Date.now() < Date.parse(current.next_run_at);
    button.disabled = working || !!error || !current?.configured || active.has(current?.status) || !!cooldown;
    button.textContent = working || ['creating', 'sending', 'running'].includes(current?.status) ? 'Scout running…' : 'Run Scout';
    refreshButton.disabled = working;
    const fixture = current?.fixture ? 'SYNTHETIC PREVIEW · NO BROKER · ' : local ? 'LOCAL · SQLITE · ' : '';
    const label = current?.error_code && Object.hasOwn(messages, current.error_code) ? messages[current.error_code] :
      current && !current.configured ? messages.scout_not_configured :
      current?.status === 'completed' ? `Scout complete · ${clock(current.report?.as_of)}${cooldown ? ` · next run after ${clock(current.next_run_at)}` : ''}` :
      active.has(current?.status) ? `Scout ${current.status} · results appear automatically · started ${clock(current.started_at)}` :
      current ? 'Run Scout for fresh research. Refresh checks this run only. No orders will be placed.' : 'Checking Scout availability…';
    statusElement.textContent = fixture + (error || label);
    if (current?.report !== lastReport || !content.innerHTML) { lastReport = current?.report ?? null; report(lastReport); }
  }

  function schedule() {
    clearTimeout(timer);
    if (!error && ['creating', 'sending', 'running'].includes(current?.status)) timer = setTimeout(sync, 5000);
    else if (!error && current?.next_run_at && Date.now() < Date.parse(current.next_run_at)) timer = setTimeout(render, Math.min(300000, Date.parse(current.next_run_at) - Date.now() + 50));
  }

  async function sync() {
    if (working || !current?.request_id) return;
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
    try { current = await requestJson('scout-status'); error = ''; }
    catch (err) { error = err.message; }
    finally { working = false; render(); }
    if (!error && (active.has(current?.status) || (reconcileCompleted && local && current?.status === 'completed' && current.report?.ticket_checks === 'not_run'))) await sync();
    else schedule();
  }

  async function start() {
    if (button.disabled || !current) return;
    clearTimeout(timer);
    working = true;
    error = '';
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
