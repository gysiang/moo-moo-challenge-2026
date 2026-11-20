import { ScoutError, scoutReport } from '../functions/scout-report.mjs';

export function artifact(run, now = Date.now()) {
  const stamp = new Date(now).toISOString();
  return {version: 1, request_id: run.request_id, mode: 'scout', account_id: '5829804', market: 100, as_of: stamp,
    cash: {ret_code: 0, data: {total_asset: 100000, balance: 99000}},
    positions: {ret_code: 0, data: {positions: [{market: 100, pstn_type: 0, symbol: 'AAPL', qty: 10, mv: 1000, cost_price: 99}]}},
    order_pages: [{ret_code: 0, data: {orders: [{order_id: 'fixture-filled', symbol: 'AAPL', side: 1, qty: 10, cum_qty: 10, price: 99, avg_fill_price: 99, status: 4, create_time: now * 1000}]}, pagination: {has_more: false}}],
    market_raw: {as_of: stamp, symbols: {'US.SPY': {instrument_type: 'etf', market_cap: 0, closes: Array.from({length: 60}, (_, i) => 41 + i), highs: Array.from({length: 60}, (_, i) => 42 + i), as_of: stamp}}},
    news: {'US.SPY': [{id: 'fixture-news', as_of: stamp, source: 'synthetic fixture', title: 'SYNTHETIC PREVIEW: broad-market research example'}]}};
}

export function approvalFixtureReport(run, now) {
  const report = scoutReport(artifact(run, now), run, now);
  const stamp = new Date(now).toISOString();
  report.ticket_checks = 'completed';
  report.cycle = {request_id: run.request_id, generated_at: stamp, evaluated_at: stamp, historical: false,
    review_only: true, trading_enabled: false, baseline_verified: true, skipped: [],
    records: ['SPY', 'QQQ', 'AAPL'].map((symbol, index) => ({
      ticket_id: `P-SYNTHETIC-${symbol}-01`, symbol, side: 'BUY', quantity: 1, limit_price: 100, est_cost: 100,
      proposal_hash: String(index + 1).repeat(64), rationale: 'Synthetic approval-control fixture; not broker data.',
      evidence: [{id: 'fixture', kind: 'news', as_of: stamp, source: 'Synthetic fixture'}],
      status: index === 2 ? 'REJECTED' : 'AWAITING_APPROVAL',
      gate: {passed: false, checked: Array.from({length: 11}, (_, i) => `R${i + 1}`),
        codes: index === 2 ? ['R9_DUPLICATE', 'R11_NO_APPROVAL'] : ['R11_NO_APPROVAL'],
        rejections: [...(index === 2 ? [{code: 'R9_DUPLICATE', message: 'Synthetic working order already exists.'}] : []),
          {code: 'R11_NO_APPROVAL', message: 'no approval on record at assessment'}]},
    }))};
  return report;
}

export function fixture({now = Date.now} = {}) {
  const saved = new Map();
  const counts = {creates: 0, sends: 0, collects: 0};
  const options = {storageError: false, sendUnknown: false, createUnknown: false, pending: false, invalid: false, closed: false, readError: false};
  const store = {
    async read(owner) { if (options.storageError) throw new Error('private database error'); return structuredClone(saved.get(owner) ?? null); },
    async insert(row) { if (options.storageError) throw new Error('private database error'); if (saved.has(row.owner_id)) return null; saved.set(row.owner_id, structuredClone(row)); return structuredClone(row); },
    async change(owner, version, values) {
      if (options.storageError) throw new Error('private database error');
      const row = saved.get(owner);
      if (!row || row.version !== version) return null;
      const changed = {...row, ...structuredClone(values), version: version + 1};
      saved.set(owner, changed);
      return structuredClone(changed);
    },
  };
  const cloud = {
    configured: () => true,
    async create() { counts.creates++; if (options.createUnknown) throw new ScoutError('dispatch_unknown'); return `sess_fixture${counts.creates}`; },
    async send() { counts.sends++; if (options.sendUnknown) throw new ScoutError('dispatch_unknown'); return ['evt_fixture']; },
    async collect(run) {
      counts.collects++;
      if (options.readError) throw new ScoutError('cloud_unavailable');
      if (options.pending) return {status: 'running'};
      const payload = artifact(run, now());
      if (options.invalid) payload.account_id = 'not-the-paper-account';
      if (options.closed) { payload.market_raw.symbols = {}; payload.news = {}; }
      return {status: 'completed', payload};
    },
  };
  const requireUser = ctx => { if (!ctx.user) throw new ScoutError('login_required', 401); return ctx.user; };
  return {store, cloud, requireUser, now, counts, options, saved};
}
