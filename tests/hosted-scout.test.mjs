import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { scoutApplication } from '../functions/scout-routes.mjs';
import { scoutReport, ema, WATCHLIST, ScoutError } from '../functions/scout-report.mjs';
import { scoutCloud } from '../functions/scout-cloud.mjs';
import { scoutStore } from '../functions/scout-store.mjs';
import { fixture, artifact } from '../dev/hosted-scout-fixture.mjs';

const REQUEST = 'a'.repeat(32), OTHER = 'b'.repeat(32);
const NOW = Date.parse('2026-11-20T15:00:00Z');
const run = {request_id: REQUEST, started_at: new Date(NOW).toISOString()};
function setup() {
  const f = fixture({now: () => NOW});
  const app = scoutApplication(f);
  const call = async (action, body, owner = 'owner-one', method = body ? 'POST' : 'GET', path = '/functions/v1/app') => {
    const request = new Request(`https://site.test${path}?action=${action}`, {method, headers: body ? {'Content-Type': 'application/json'} : {}, ...(body ? {body: JSON.stringify(body)} : {})});
    const res = await app({request, user: owner ? {id: owner} : null});
    return {status: res.status, data: await res.json()};
  };
  return {...f, app, call, start: (request_id = REQUEST, version = 0) => call('scout-start', {request_id, version}), sync: () => call('scout-sync', {request_id: REQUEST})};
}

test('page load and status reads never create or send a Scout', async () => {
  const f = setup();
  assert.equal((await f.call('scout-status')).data.status, 'idle');
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});
test('one click, one dispatch, validated report and retained result after reload', async () => {
  const f = setup();
  assert.equal((await f.start()).data.status, 'running');
  const done = await f.sync();
  assert.equal(done.data.status, 'completed');
  assert.equal(done.data.report.symbols[0].trend_up, true);
  assert.equal(done.data.report.ticket_checks, 'not_run');
  assert.equal(done.data.trading_enabled, false);
  assert.equal(done.data.report.account.filled_orders.length, 1);
  assert.equal(done.data.report.account.open_orders.length, 0);
  assert.equal((await f.call('scout-status')).data.report.as_of, run.started_at);
  assert.deepEqual(f.counts, {creates: 1, sends: 1, collects: 1});
  assert.ok(!JSON.stringify(done).includes('sess_fixture'));
  assert.ok(!JSON.stringify(done).includes('owner-one'));
});
test('simultaneous clicks cannot dispatch twice', async () => {
  const f = setup();
  await Promise.all([f.start(), f.start(OTHER)]);
  assert.equal(f.counts.creates, 1);
  assert.equal(f.counts.sends, 1);
});
test('same intent is never resent, including after completion', async () => {
  const f = setup();
  await f.start(); await f.sync(); await f.start();
  assert.equal(f.counts.creates, 1);
  assert.equal(f.counts.sends, 1);
});
test('lost creation response blocks new sessions and survives handler restart', async () => {
  const f = setup(); f.options.createUnknown = true;
  assert.equal((await f.start()).data.status, 'unknown');
  assert.equal((await f.start(OTHER, 2)).status, 409);
  const app = scoutApplication(f);
  const res = await app({request: new Request('https://site.test/functions/v1/app?action=scout-status'), user: {id: 'owner-one'}});
  assert.equal((await res.json()).status, 'unknown');
  assert.equal(f.counts.creates, 1);
});
test('lost send response reconciles through artifact without resending', async () => {
  const f = setup(); f.options.sendUnknown = true;
  assert.equal((await f.start()).data.status, 'unknown');
  assert.equal((await f.sync()).data.status, 'completed');
  assert.equal(f.counts.sends, 1);
});
test('failure to persist session prevents sending', async () => {
  const f = setup();
  f.cloud.create = async () => { f.options.storageError = true; return 'sess_fixture'; };
  assert.equal((await f.start()).status, 503);
  assert.equal(f.counts.sends, 0);
});
test('lost persistence response is not a reason to repeat dispatch', async () => {
  const f = setup(), original = f.store.change;
  let calls = 0;
  f.store.change = async (...args) => { const result = await original(...args); if (++calls === 1) throw new Error('response lost'); return result; };
  await f.start();
  assert.equal(f.counts.sends, 0);
  assert.equal((await f.start(OTHER)).status, 409);
});
test('anonymous and wrong-owner requests do not access another run', async () => {
  const f = setup();
  assert.equal((await f.call('scout-start', {request_id: REQUEST, version: 0}, null)).status, 401);
  await f.start();
  assert.equal((await f.call('scout-status', undefined, 'owner-two')).data.status, 'idle');
  assert.equal((await f.call('scout-sync', {request_id: REQUEST}, 'owner-two')).status, 404);
  assert.equal(f.counts.collects, 0);
});
test('routing rejects methods and arbitrary Guard/trade operations', async () => {
  const f = setup();
  assert.equal((await f.call('scout-start')).status, 405);
  assert.equal((await f.call('approve', {})).status, 404);
  assert.equal((await f.call('scout-status', undefined, 'owner-one', 'GET', '/functions/v1/app/guard')).status, 404);
  assert.equal((await f.call('scout-status', undefined, 'owner-one', 'GET', '/functions/v1/app-d' + 'a'.repeat(32))).status, 200);
  assert.equal(f.counts.creates, 0);
});
test('input does not accept visitor owner IDs, URLs, templates or mode overrides', async () => {
  const f = setup();
  for (const key of ['owner_id', 'url', 'template_id', 'mode']) assert.equal((await f.call('scout-start', {request_id: REQUEST, version: 0, [key]: 'untrusted'})).status, 400);
  assert.equal((await f.call('scout-start', {request_id: 'bad', version: 0})).status, 400);
  assert.equal((await f.call('scout-start', {request_id: REQUEST, version: 0, padding: 'x'.repeat(1500)})).status, 400);
  assert.equal(f.counts.creates, 0);
});
test('read error preserves existing run and does not resend', async () => {
  const f = setup(); await f.start(); f.options.readError = true;
  assert.equal((await f.sync()).status, 503);
  assert.equal((await f.call('scout-status')).data.status, 'running');
  assert.equal(f.counts.sends, 1);
});
test('invalid artifact cannot replace previous report', async () => {
  const f = setup(); await f.start(); await f.sync();
  const row = f.saved.get('owner-one'), report = structuredClone(row.last_report);
  row.status = 'running'; f.options.invalid = true;
  const result = await f.sync();
  assert.equal(result.data.status, 'failed');
  assert.equal(result.data.error_code, 'invalid_scout_result');
  assert.deepEqual(result.data.report, report);
});
test('concurrent collectors use version fencing', async () => {
  const f = setup(); await f.start();
  await Promise.all([f.sync(), f.sync()]);
  assert.equal(f.saved.get('owner-one').version, 4);
  assert.equal(f.counts.sends, 1);
});
test('new run requires current version and respects cooldown', async () => {
  const f = setup(); await f.start(); await f.sync();
  assert.equal((await f.start(OTHER, 0)).status, 409);
  assert.equal((await f.start(OTHER, 4)).status, 429);
});
test('expired unresolved run remains blocked', async () => {
  const f = setup(); await f.start();
  f.saved.get('owner-one').started_at = new Date(NOW - 1900000).toISOString();
  assert.equal((await f.call('scout-status')).data.status, 'unknown');
  assert.equal((await f.start(OTHER, 3)).status, 409);
});
test('closed/empty research is valid but never invents a market-open flag', async () => {
  const f = setup(); f.options.closed = true; await f.start();
  const result = (await f.sync()).data.report;
  assert.deepEqual(result.symbols, []);
  assert.equal(result.omitted_symbols.length, WATCHLIST.length);
  assert.equal(result.market_state, undefined);
});
for (const [name, mutate] of [
  ['wrong request', p => { p.request_id = OTHER; }],
  ['wrong account', p => { p.account_id = 'live'; }],
  ['stale collection', p => { p.as_of = '2026-11-19T15:00:00Z'; }],
  ['naive timestamp', p => { p.as_of = '2026-11-20T15:00:00'; }],
  ['NaN balance', p => { p.cash.data.balance = 'NaN'; }],
  ['incomplete history', p => { p.order_pages[0].pagination.has_more = true; }],
  ['duplicate order', p => { p.order_pages[0].data.orders.push({...p.order_pages[0].data.orders[0]}); }],
  ['fractional quantity', p => { p.positions.data.positions[0].qty = 0.5; }],
  ['foreign position', p => { p.positions.data.positions[0].market = 200; }],
  ['overfilled order', p => { p.order_pages[0].data.orders[0].cum_qty = 11; }],
  ['unknown symbol', p => { p.market_raw.symbols['US.INVALID'] = p.market_raw.symbols['US.SPY']; }],
  ['insufficient history', p => { p.market_raw.symbols['US.SPY'].closes = [1, 2]; }],
  ['future news', p => { p.news['US.SPY'][0].as_of = '2099-01-01T00:00:00Z'; }],
]) test(`rejects ${name}`, () => { const p = artifact(run, NOW); mutate(p); assert.throws(() => scoutReport(p, run, NOW)); });

test('EMA matches the existing seeded simple-average calculation', () => {
  const closes = Array.from({length: 60}, (_, i) => 41 + i);
  assert.ok(Math.abs(ema(closes, 8) - 96.5) < 0.000001);
  assert.ok(Math.abs(ema(closes, 21) - 90) < 0.000001);
});
test('hosted watchlist stays equal to the local strategy watchlist', async () => {
  const watch = JSON.parse(await readFile(new URL('../scout/watchlist.json', import.meta.url)));
  assert.deepEqual(WATCHLIST, [...watch.core.map(row => row.symbol), ...watch.satellites]);
});
test('cloud transport fixes template and request, omits broker-write endpoints', async () => {
  const calls = [];
  const cloud = scoutCloud({getToken: () => 'fixture-not-a-token', fetcher: async (url, init) => { calls.push({url, init}); return Response.json(url.endsWith('/sessions') ? {id: 'sess_fixture'} : {data: [{id: 'evt_fixture'}]}); }});
  const session_id = await cloud.create(run); await cloud.send({...run, session_id});
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, 'https://api.qoder.com/api/v1/forward/sessions');
  assert.equal(JSON.parse(calls[0].init.body).template_id, 'tmpl_834e2027a979a145b432ff03');
  const event = JSON.parse(calls[1].init.body).events[0];
  assert.equal(event.type, 'user.message');
  assert.equal(JSON.parse(event.content[0].text).mode, 'scout');
  assert.ok(!event.content[0].text.includes('fixture-not-a-token'));
  assert.ok(calls.every(call => call.init.redirect === 'error'));
});
test('unconfirmed cloud POST is never retried and returns no raw exception', async () => {
  let calls = 0;
  const cloud = scoutCloud({getToken: () => 'fixture-not-a-token', fetcher: async () => { calls++; throw new Error('fixture-not-a-token'); }});
  await assert.rejects(() => cloud.create(run), error => error.code === 'dispatch_unknown' && !error.message.includes('fixture-not-a-token'));
  assert.equal(calls, 1);
});
test('missing platform token prevents any transport', async () => {
  let calls = 0;
  const cloud = scoutCloud({getToken: () => undefined, fetcher: async () => { calls++; }});
  await assert.rejects(() => cloud.create(run), error => error.code === 'scout_not_configured');
  assert.equal(calls, 0);
});
test('database update binds owner and version in the same query', async () => {
  const trace = [];
  const query = {update(value) {trace.push(['update', value]); return this;}, eq(...args) {trace.push(['eq', ...args]); return this;}, async select() {return {data: [{version: 8}], error: null};}};
  const store = scoutStore({from: name => {assert.equal(name, 'scout_state'); return query;}});
  await store.change('owner-one', 7, {status: 'completed'});
  assert.deepEqual(trace, [['update', {status: 'completed', version: 8}], ['eq', 'owner_id', 'owner-one'], ['eq', 'version', 7]]);
});
