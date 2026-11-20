export class ScoutError extends Error {
  constructor(code, status = 503) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

export const WATCHLIST = ['US.SPY', 'US.QQQ', 'US.IWM', 'US.AAPL', 'US.MSFT', 'US.NVDA', 'US.AMZN', 'US.GOOGL', 'US.META', 'US.AVGO', 'US.TSLA', 'US.JPM', 'US.V', 'US.UNH', 'US.XOM', 'US.LLY', 'US.COST', 'US.HD', 'US.PG', 'US.JNJ', 'US.WMT'];
const invalid = () => { throw new ScoutError('invalid_scout_result', 422); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const list = (value, limit) => { if (!Array.isArray(value) || value.length > limit) invalid(); return value; };
const text = (value, limit) => { if (typeof value !== 'string' || !value.trim() || value.length > limit) invalid(); return value; };
const num = value => {
  if (typeof value !== 'number' && !(typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value))) invalid();
  const result = Number(value);
  if (!Number.isFinite(result)) invalid();
  return result;
};
const qty = value => { const n = num(value); if (!Number.isSafeInteger(n) || n < 0) invalid(); return n; };
const time = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) invalid();
  return Date.parse(value);
};
const data = value => {
  if (!object(value) || ![0, '0'].includes(value.ret_code) || !object(value.data)) invalid();
  return value.data;
};
const symbol = value => {
  if (typeof value !== 'string' || !/^(?:US\.)?[A-Z0-9][A-Z0-9.\-]{0,24}$/.test(value)) invalid();
  return value.startsWith('US.') ? value : `US.${value}`;
};
export function ema(closes, period) {
  let result = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;
  const weight = 2 / (period + 1);
  for (const close of closes.slice(period)) result = close * weight + result * (1 - weight);
  return result;
}
export function orderStatus(raw) {
  const codes = {1: 'ACKED', 2: 'ACKED', 3: 'PARTIAL', 4: 'FILLED', 5: 'PARTIAL', 6: 'PARTIAL_CANCELLED', 7: 'CANCELLED', 8: 'REJECTED', 9: 'REJECTED', 10: 'CANCELLED', 11: 'ACKED', 12: 'REJECTED', 13: 'UNKNOWN'};
  if (typeof raw === 'number' || (typeof raw === 'string' && /^\d+$/.test(raw))) return codes[raw] || 'UNKNOWN';
  const s = typeof raw === 'string' ? raw.toUpperCase() : '';
  if (s.includes('PART') && s.includes('CANCEL')) return 'PARTIAL_CANCELLED';
  if (s.includes('PART') && s.includes('FILL')) return 'PARTIAL';
  if (/CANCEL|DELET|DISABLE/.test(s)) return 'CANCELLED';
  if (/FAIL|REJECT/.test(s)) return 'REJECTED';
  if (s.includes('FILL')) return 'FILLED';
  return /SUBMIT|WAIT|ACK/.test(s) ? 'ACKED' : 'UNKNOWN';
}

export function scoutReport(payload, run, now = Date.now()) {
  if (!object(payload) || payload.version !== 1 || payload.request_id !== run.request_id || payload.mode !== 'scout' || payload.account_id !== '5829804' || payload.market !== 100) invalid();
  const fresh = value => {
    const stamp = time(value);
    if (stamp < time(run.started_at) - 5000 || stamp > now + 5000 || now - stamp > 1800000) invalid();
    return new Date(stamp).toISOString();
  };
  const as_of = fresh(payload.as_of);
  const cash = data(payload.cash);
  const equity = num(cash.total_asset), balance = num(cash.balance);
  if (equity <= 0) invalid();
  const seenPositions = new Set();
  const positions = list(data(payload.positions).positions, 100).map(row => {
    if (!object(row) || row.market !== 100 || row.pstn_type !== 0) invalid();
    const key = symbol(row.symbol);
    if (seenPositions.has(key)) invalid();
    seenPositions.add(key);
    return {symbol: key, quantity: qty(row.qty), market_value: num(row.mv), average_cost: num(row.cost_price)};
  });
  const pages = list(payload.order_pages, 100);
  if (!pages.length) invalid();
  const ids = new Set(), orders = [];
  pages.forEach((page, index) => {
    if (page?.pagination?.has_more !== (index < pages.length - 1)) invalid();
    for (const row of list(data(page).orders, 200)) {
      if (!object(row) || !['1', '2'].includes(String(row.side))) invalid();
      if (!['string', 'number'].includes(typeof row.order_id)) invalid();
      const id = text(String(row.order_id), 80);
      if (ids.has(id)) invalid();
      ids.add(id);
      const quantity = qty(row.qty), filled_quantity = qty(row.cum_qty);
      if (filled_quantity > quantity) invalid();
      const created = new Date(num(row.create_time) / 1000);
      if (!Number.isFinite(created.getTime())) invalid();
      orders.push({order_id: id, symbol: symbol(row.symbol), side: String(row.side) === '1' ? 'BUY' : 'SELL', quantity, filled_quantity,
        limit_price: num(row.price), average_fill_price: row.avg_fill_price == null ? null : num(row.avg_fill_price), status: orderStatus(row.status), created_at: created.toISOString()});
    }
  });
  const raw = payload.market_raw;
  if (!object(raw) || !object(raw.symbols) || !object(payload.news)) invalid();
  fresh(raw.as_of);
  if (Object.keys(raw.symbols).some(key => !WATCHLIST.includes(key)) || Object.keys(payload.news).some(key => !Object.hasOwn(raw.symbols, key))) invalid();
  const symbols = Object.entries(raw.symbols).map(([key, row]) => {
    if (!object(row) || !['stock', 'etf'].includes(row.instrument_type)) invalid();
    const closes = list(row.closes, 1000).map(num), highs = list(row.highs, 1000).map(num);
    if (closes.length < 50 || highs.length < 20 || closes.some(v => v <= 0) || highs.some(v => v <= 0)) invalid();
    const market_cap = num(row.market_cap);
    if (market_cap < 0) invalid();
    const news = list(payload.news[key] ?? [], 3).map(item => {
      if (!object(item) || time(item.as_of) > now + 5000) invalid();
      return {id: text(item.id, 200), as_of: new Date(time(item.as_of)).toISOString(), source: text(item.source, 100), title: text(item.title, 500)};
    });
    const ema8 = ema(closes, 8), ema21 = ema(closes, 21);
    return {symbol: key, instrument_type: row.instrument_type, market_cap, as_of: fresh(row.as_of ?? raw.as_of), last_price: closes.at(-1),
      ema8, ema21, trend_up: ema8 > ema21, sma50: closes.slice(-50).reduce((a, b) => a + b, 0) / 50,
      high20: Math.max(...highs.slice(-20)), roc20: closes.at(-1) / closes.at(-21) - 1, news};
  });
  const working = orders.filter(row => ['ACKED', 'PARTIAL', 'UNKNOWN'].includes(row.status));
  if (working.length > 200) invalid();
  return {request_id: run.request_id, as_of, account: {cash: balance, equity, positions, orders_complete: true,
    open_orders: working, filled_orders: orders.filter(row => row.filled_quantity > 0).sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 100)},
    symbols, omitted_symbols: WATCHLIST.filter(key => !Object.hasOwn(raw.symbols, key)),
    trading_enabled: false, ticket_checks: 'not_run', baseline_verified: false};
}
