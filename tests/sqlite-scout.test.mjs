import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, rm, writeFile, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {request as httpRequest} from 'node:http';
import {sqliteScoutStore} from '../dashboard/scout-store.mjs';
import {loadScoutToken, startScoutServer} from '../dashboard/scout-server.mjs';
import {scoutCloud} from '../functions/scout-cloud.mjs';
import {scoutApplication} from '../functions/scout-routes.mjs';
import {execFile, spawn} from 'node:child_process';
import {createServer} from 'node:net';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';
import {artifact, fixture, approvalFixtureReport} from '../dev/hosted-scout-fixture.mjs';
import {scoutApprovals} from '../dashboard/scout-approvals.mjs';
import {ticketEvaluator} from '../dashboard/scout-tickets.mjs';
import {scoutBaselines} from '../dashboard/scout-baselines.mjs';
import {createHash} from 'node:crypto';
import {scoutReport} from '../functions/scout-report.mjs';

const ID = 'a'.repeat(32), NEXT = 'b'.repeat(32);
const NOW = Date.parse('2026-11-20T15:00:00Z');
const initial = {owner_id: 'owner', version: 1, request_id: ID, started_at: new Date(NOW).toISOString(), status: 'creating', session_id: null, event_ids: [], completed_at: null, error_code: null, last_report: null};
async function workspace(t) {
  const root = await mkdtemp(join(tmpdir(), 'scout-sqlite-'));
  const cleanup = [];
  t.after(async () => { for (const dispose of cleanup.reverse()) await dispose(); await rm(root, {recursive: true, force: true}); });
  return {root, path: join(root, 'scout.sqlite3'), cleanup};
}
function client(store, f) {
  const app = scoutApplication({...f, store});
  return async (action, body) => {
    const request = new Request(`http://127.0.0.1/functions/v1/app?action=${action}`, {method: body ? 'POST' : 'GET', headers: {'Content-Type': 'application/json'}, ...(body ? {body: JSON.stringify(body)} : {})});
    const response = await app({request, user: {id: 'owner'}});
    return {status: response.status, data: await response.json()};
  };
}
async function web(root) {
  const directory = join(root, 'web');
  await mkdir(directory);
  await writeFile(join(directory, 'index.html'), '<html data-static="1" data-hosted-scout="1"></html>');
  for (const name of ['app.js', 'hosted-scout.js', 'style.css']) await writeFile(join(directory, name), '');
  for (const name of ['state.json', 'cloud.json']) await writeFile(join(directory, name), '{}');
  return directory;
}
const stop = server => new Promise((accept, reject) => server.close(error => error ? reject(error) : accept()));
async function httpCall(url, action, body, headers = {}) {
  return fetch(`${url}/functions/v1/app?action=${action}`, {method: body ? 'POST' : 'GET', headers: {...(body ? {'Content-Type': 'application/json', Origin: url} : {}), ...headers}, ...(body ? {body: JSON.stringify(body)} : {})});
}

test('SQLite stores state and reports durably across close and reopen', async t => {
  const w = await workspace(t);
  let store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  assert.deepEqual(await store.insert(initial), initial);
  const report = {account: {cash: 99}, symbols: [{symbol: 'US.SPY'}]};
  await store.change('owner', 1, {status: 'completed', event_ids: ['evt_1'], last_report: report});
  store.close(); store = sqliteScoutStore(w.path);
  const saved = await store.read('owner');
  assert.equal(saved.version, 2);
  assert.deepEqual(saved.last_report, report);
  assert.deepEqual(saved.event_ids, ['evt_1']);
  assert.equal(await store.read('other'), null);
  assert.equal((await readFile(w.path)).subarray(0, 15).toString(), 'SQLite format 3');
});

test('two database connections enforce unique insert and version fencing', async t => {
  const w = await workspace(t), a = sqliteScoutStore(w.path), b = sqliteScoutStore(w.path);
  w.cleanup.push(() => a.close(), () => b.close());
  const inserted = await Promise.all([a.insert(initial), b.insert({...initial, request_id: NEXT})]);
  assert.equal(inserted.filter(Boolean).length, 1);
  const changed = await Promise.all([a.change('owner', 1, {status: 'running'}), b.change('owner', 1, {status: 'failed'})]);
  assert.equal(changed.filter(Boolean).length, 1);
  assert.equal((await a.read('owner')).version, 2);
  assert.equal(await b.change('other', 2, {status: 'failed'}), null);
});

test('dynamic updates reject fields and bind SQL-looking values as data', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  await store.insert({...initial, owner_id: "owner'; DROP TABLE scout_state; --"});
  await assert.rejects(store.change(initial.owner_id, 1, {'owner_id = ?; DROP TABLE scout_state; --': 'x'}), {code: 'scout_storage_unavailable'});
  assert.equal((await store.read("owner'; DROP TABLE scout_state; --")).version, 1);
  assert.equal(await store.read('owner'), null);
});

test('invalid state updates roll back without replacing the saved row', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  await store.insert(initial);
  await assert.rejects(store.change('owner', 1, {status: 'APPROVED'}), {code: 'scout_storage_unavailable'});
  assert.deepEqual(await store.read('owner'), initial);
});

test('unrelated and corrupt databases fail closed without replacement', async t => {
  const w = await workspace(t), db = new DatabaseSync(w.path);
  db.exec('CREATE TABLE existing (value TEXT); INSERT INTO existing VALUES (\'keep\')'); db.close();
  const before = await readFile(w.path);
  assert.throws(() => sqliteScoutStore(w.path), {code: 'scout_storage_unavailable'});
  assert.deepEqual(await readFile(w.path), before);
  const corrupt = join(w.root, 'corrupt.sqlite3');
  await writeFile(corrupt, 'not a database');
  assert.throws(() => sqliteScoutStore(corrupt), {code: 'scout_storage_unavailable'});
  assert.equal(await readFile(corrupt, 'utf8'), 'not a database');
});

test('concurrent application starts dispatch exactly once with SQLite', async t => {
  const w = await workspace(t), a = sqliteScoutStore(w.path), b = sqliteScoutStore(w.path), f = fixture({now: () => NOW});
  w.cleanup.push(() => a.close(), () => b.close());
  await Promise.all([client(a, f)('scout-start', {request_id: ID, version: 0}), client(b, f)('scout-start', {request_id: NEXT, version: 0})]);
  assert.deepEqual(f.counts, {creates: 1, sends: 1, collects: 0});
});

test('uncertain dispatch survives restart and reconciles without resending', async t => {
  const w = await workspace(t), f = fixture({now: () => NOW});
  let store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  f.options.sendUnknown = true;
  assert.equal((await client(store, f)('scout-start', {request_id: ID, version: 0})).data.status, 'unknown');
  store.close(); store = sqliteScoutStore(w.path);
  const call = client(store, f);
  assert.equal((await call('scout-status')).data.status, 'unknown');
  assert.equal((await call('scout-start', {request_id: NEXT, version: 3})).status, 409);
  assert.equal((await call('scout-sync', {request_id: ID})).data.status, 'completed');
  assert.deepEqual(f.counts, {creates: 1, sends: 1, collects: 1});
});

test('storage failure after create prevents send and remains blocked on restart', async t => {
  const w = await workspace(t), f = fixture({now: () => NOW});
  let store = sqliteScoutStore(w.path);
  const create = f.cloud.create;
  f.cloud.create = async run => { const session = await create(run); store.close(); return session; };
  assert.equal((await client(store, f)('scout-start', {request_id: ID, version: 0})).status, 503);
  store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  assert.equal((await client(store, f)('scout-start', {request_id: NEXT, version: 1})).status, 409);
  assert.deepEqual(f.counts, {creates: 1, sends: 0, collects: 0});
});

test('a later invalid result retains the previous report in SQLite', async t => {
  let clock = NOW;
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => clock});
  w.cleanup.push(() => store.close());
  const call = client(store, f);
  await call('scout-start', {request_id: ID, version: 0});
  const first = (await call('scout-sync', {request_id: ID})).data;
  clock += 300001;
  await call('scout-start', {request_id: NEXT, version: first.version});
  f.options.invalid = true;
  const failed = (await call('scout-sync', {request_id: NEXT})).data;
  assert.equal(failed.status, 'failed');
  assert.deepEqual(failed.report, first.report);
  assert.equal(failed.trading_enabled, false);
});

test('local HTTP server resumes saved report after server restart', async t => {
  const w = await workspace(t), directory = await web(w.root), f = fixture();
  let running = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud: f.cloud, port: 0});
  w.cleanup.push(() => stop(running.server));
  assert.equal((await httpCall(running.url, 'scout-start', {request_id: ID, version: 0})).status, 202);
  const done = await (await httpCall(running.url, 'scout-sync', {request_id: ID})).json();
  await stop(running.server);
  running = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud: f.cloud, port: 0});
  const resumed = await (await httpCall(running.url, 'scout-status')).json();
  assert.deepEqual(resumed.report, done.report);
  assert.equal(resumed.report.ticket_checks, 'completed');
  assert.equal(resumed.report.cycle.review_only, true);
  assert.equal(resumed.report.cycle.trading_enabled, false);
  assert.equal(resumed.status, 'completed');
  assert.deepEqual(f.counts, {creates: 1, sends: 1, collects: 1});
  assert.equal(resumed.trading_enabled, false);
  assert.match(await (await fetch(running.url)).text(), /data-local-scout="1"/);
});

test('local HTTP rejects cross-origin requests, wrong hosts and oversized bodies', async t => {
  const w = await workspace(t), directory = await web(w.root), f = fixture();
  const {server, url} = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud: f.cloud, port: 0});
  w.cleanup.push(() => stop(server));
  assert.equal((await httpCall(url, 'scout-start', {request_id: ID, version: 0}, {Origin: 'https://untrusted.test'})).status, 403);
  assert.equal((await fetch(`${url}/functions/v1/app?action=scout-start`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})).status, 403);
  const wrongHostStatus = await new Promise((accept, reject) => {
    const request = httpRequest(`${url}/functions/v1/app?action=scout-status`, {headers: {Host: 'untrusted.test'}}, response => { response.resume(); accept(response.statusCode); });
    request.on('error', reject); request.end();
  });
  assert.equal(wrongHostStatus, 403);
  assert.equal((await httpCall(url, 'scout-status', null, {'Sec-Fetch-Site': 'cross-site'})).status, 403);
  assert.equal((await httpCall(url, 'scout-start', {padding: 'a'.repeat(2000)})).status, 413);
  assert.equal((await fetch(url, {method: 'PUT'})).status, 405);
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});

test('HTTP serves neither SQLite files, private source, nor trading endpoints', async t => {
  const w = await workspace(t), directory = await web(w.root), f = fixture();
  const {server, url} = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud: f.cloud, port: 0});
  w.cleanup.push(() => stop(server));
  for (const path of ['/scout.sqlite3', '/scout.sqlite3-wal', '/.env', '/%2e%2e%2fscout.sqlite3', '/fixture/setup', '/api/approve', '/api/guard']) assert.equal((await fetch(url + path)).status, 404, path);
  assert.equal((await httpCall(url, 'approve', {request_id: ID})).status, 404);
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
  await assert.rejects(startScoutServer({databasePath: join(directory, 'scout.sqlite3'), webDirectory: directory, cloud: f.cloud, port: 0}), /outside the served directory/);
});

test('SQLite commits Python gate decisions with the report and retains them after a later evaluator failure', async t => {
  let clock = NOW;
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => clock});
  w.cleanup.push(() => store.close());
  const evaluate = ticketEvaluator();
  let broken = false, evaluations = 0;
  f.evaluateTickets = async (...args) => { evaluations++; return (broken ? ticketEvaluator(join(w.root, 'missing-python')) : evaluate)(...args); };
  f.approvals = scoutApprovals({store, now: () => clock});
  const call = client(store, f);
  await call('scout-start', {request_id: ID, version: 0});
  const first = (await call('scout-sync', {request_id: ID})).data;
  assert.equal(first.report.ticket_checks, 'completed');
  assert.equal(first.report.cycle.records[0].status, 'REJECTED');
  assert.ok(first.report.cycle.records[0].gate.codes.includes('R8_DAILY_BASELINE_UNVERIFIED'));
  assert.ok(first.report.cycle.records[0].gate.rejections.every(item => item.message));
  assert.equal(first.report.cycle.request_id, ID);
  assert.equal(first.report.cycle.historical, false);
  assert.equal(first.approvals.tickets[0].eligible, false);
  assert.equal((await call('scout-approve', approvalBody(first.report, [0], first.version))).data.error, 'ticket_not_eligible');
  assert.deepEqual(await store.approvals('owner', ID), []);
  await call('scout-status'); await call('scout-sync', {request_id: ID});
  assert.equal(evaluations, 1);
  clock += 300001; broken = true;
  await call('scout-start', {request_id: NEXT, version: first.version});
  const failed = await call('scout-sync', {request_id: NEXT});
  assert.equal(failed.status, 503);
  assert.equal(failed.data.error, 'ticket_generation_unavailable');
  assert.deepEqual((await call('scout-status')).data.report, first.report);
  broken = false;
  const retried = (await call('scout-sync', {request_id: NEXT})).data;
  assert.equal(retried.report.cycle.request_id, NEXT);
  assert.deepEqual(f.counts, {creates: 2, sends: 2, collects: 3});
});

test('explicit refresh backfills completed research once without new dispatch or freshening timestamps', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => NOW + 86400000});
  w.cleanup.push(() => store.close());
  const payload = artifact(initial, NOW), report = scoutReport(payload, initial, NOW);
  await store.insert({...initial, status: 'completed', session_id: 'sess_old', completed_at: new Date(NOW).toISOString(), last_report: report});
  f.evaluateTickets = ticketEvaluator();
  f.cloud.collect = async () => { f.counts.collects++; return {status: 'completed', payload}; };
  const call = client(store, f);
  assert.equal((await call('scout-status')).data.report.ticket_checks, 'not_run');
  assert.equal(f.counts.collects, 0);
  const refreshed = (await call('scout-sync', {request_id: ID})).data;
  assert.equal(refreshed.status, 'completed');
  assert.equal(refreshed.report.ticket_checks, 'completed');
  assert.equal(refreshed.report.cycle.historical, true);
  assert.equal(Date.parse(refreshed.report.cycle.generated_at), NOW);
  assert.equal(Date.parse(refreshed.report.cycle.evaluated_at), NOW + 86400000);
  assert.equal(refreshed.report.as_of, report.as_of);
  assert.equal(refreshed.completed_at, new Date(NOW).toISOString());
  assert.ok(refreshed.report.cycle.records[0].ticket_id.startsWith('P-20261120-1000-'));
  await call('scout-sync', {request_id: ID});
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 1});
});

test('changed or missing historical artifact retains the completed report and never restarts Scout', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => NOW});
  w.cleanup.push(() => store.close());
  const payload = artifact(initial, NOW), report = scoutReport(payload, initial, NOW);
  await store.insert({...initial, status: 'completed', session_id: 'sess_old', completed_at: new Date(NOW).toISOString(), last_report: report});
  f.evaluateTickets = async () => { assert.fail('Changed historical evidence must not reach evaluator'); };
  payload.cash.data.balance++;
  f.cloud.collect = async () => ({status: 'completed', payload});
  const call = client(store, f);
  assert.equal((await call('scout-sync', {request_id: ID})).data.error, 'invalid_scout_result');
  assert.equal((await store.read('owner')).status, 'completed');
  assert.deepEqual((await store.read('owner')).last_report, report);
  f.cloud.collect = async () => ({status: 'failed'});
  assert.equal((await call('scout-sync', {request_id: ID})).data.error, 'cloud_unavailable');
  assert.deepEqual((await store.read('owner')).last_report, report);
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});

test('concurrent ticket collectors cannot overwrite a newer request or dispatch additional work', async t => {
  const w = await workspace(t), a = sqliteScoutStore(w.path), b = sqliteScoutStore(w.path), f = fixture({now: () => NOW});
  w.cleanup.push(() => a.close(), () => b.close());
  f.evaluateTickets = ticketEvaluator();
  const callA = client(a, f), callB = client(b, f);
  await callA('scout-start', {request_id: ID, version: 0});
  await Promise.all([callA('scout-sync', {request_id: ID}), callB('scout-sync', {request_id: ID})]);
  assert.equal((await a.read('owner')).version, 4);
  assert.equal((await a.read('owner')).last_report.cycle.request_id, ID);
  assert.equal(f.counts.creates, 1);
  assert.equal(f.counts.sends, 1);
});

function openingBody(changes = {}) {
  return {confirmed: true, source_text: JSON.stringify({version: 1, kind: 'broker_opening_equity',
    account_id: '5829804', market: 100, currency: 'USD', trading_day: '2026-11-20',
    as_of: '2026-11-20T09:30:00-05:00', opening_equity: '100000',
    source_reference: 'SYNTHETIC broker opening fixture', source_evidence: 'SYNTHETIC equity USD 100000 at the 09:30 ET open for account 5829804.', ...changes}, null, 2)};
}
async function baselineWorkspace(t, now = () => NOW) {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now});
  w.cleanup.push(() => store.close());
  f.baselines = scoutBaselines({store, now});
  f.evaluateTickets = ticketEvaluator();
  f.approvals = scoutApprovals({store, now});
  return {...w, store, f, call: client(store, f)};
}

test('opening import preserves exact evidence and hash, is immutable and isolated across restart', async t => {
  const w = await baselineWorkspace(t), body = openingBody();
  const first = await w.call('scout-import-opening', body);
  assert.equal(first.status, 200);
  assert.equal(first.data.status, 'idle');
  assert.equal(first.data.trading_enabled, false);
  const saved = await w.store.baseline('owner', '2026-11-20');
  assert.equal(saved.source_text, body.source_text);
  assert.equal(saved.sha256, createHash('sha256').update(body.source_text).digest('hex'));
  assert.equal(saved.verification, 'operator_attested');
  assert.equal(first.data.baselines.current.sha256, saved.sha256);
  assert.equal(first.data.baselines.current.source_text, undefined);
  assert.equal(await w.store.read('owner'), null);
  assert.deepEqual(await w.store.approvals('owner', ID), []);
  assert.equal((await w.call('scout-import-opening', body)).status, 200);
  assert.equal((await w.call('scout-import-opening', openingBody({opening_equity: '100001'}))).data.error, 'opening_record_conflict');
  assert.deepEqual(await w.store.baseline('owner', '2026-11-20'), saved);
  const reopened = sqliteScoutStore(w.path);
  try {
    assert.deepEqual(await reopened.baseline('owner', '2026-11-20'), saved);
    assert.equal(await reopened.baseline('other', '2026-11-20'), null);
    assert.equal(await reopened.baseline('owner', '2026-11-19'), null);
  } finally { reopened.close(); }
  const db = new DatabaseSync(w.path);
  try {
    assert.throws(() => db.exec("UPDATE scout_baselines SET evidence = '{}'"), /immutable/);
    assert.throws(() => db.exec('DELETE FROM scout_baselines'), /immutable/);
  } finally { db.close(); }
  assert.deepEqual(w.f.counts, {creates: 0, sends: 0, collects: 0});
});

test('invalid or unconfirmed imports write nothing and never call cloud', async t => {
  const w = await baselineWorkspace(t);
  for (const body of [null, [], {...openingBody(), confirmed: false}, {...openingBody(), token: 'not-a-credential'},
    openingBody({account_id: 'wrong'}), openingBody({as_of: '2026-11-20T10:00:00-05:00'}),
    openingBody({trading_day: '2026-11-19'}), openingBody({opening_equity: 'NaN'}),
    {confirmed: true, source_text: 'x'.repeat(17000)}]) {
    await assert.rejects(w.f.baselines.import('owner', body), {code: 'invalid_opening_record'});
  }
  assert.equal(await w.store.baseline('owner', '2026-11-20'), null);
  assert.deepEqual(w.f.counts, {creates: 0, sends: 0, collects: 0});
});

test('concurrent conflicting opening imports cannot replace the first accepted evidence', async t => {
  const w = await baselineWorkspace(t), second = sqliteScoutStore(w.path);
  try {
    const results = await Promise.all([w.call('scout-import-opening', openingBody()),
      client(second, {...w.f, baselines: scoutBaselines({store: second, now: () => NOW})})('scout-import-opening', openingBody({opening_equity: '99999'}))]);
    assert.deepEqual(results.map(r => r.status).sort(), [200, 409]);
    assert.ok(['100000', '99999'].includes((await w.store.baseline('owner', '2026-11-20')).record.opening_equity));
  } finally { second.close(); }
});

test('new Scout uses matching baseline for R8 but still needs explicit approval', async t => {
  const w = await baselineWorkspace(t), collect = w.f.cloud.collect;
  w.f.cloud.collect = async run => {
    const result = await collect(run);
    result.payload.market_raw.symbols['US.SPY'].market_cap = 1e12;
    return result;
  };
  await w.call('scout-import-opening', openingBody());
  await w.call('scout-start', {request_id: ID, version: 0});
  const result = (await w.call('scout-sync', {request_id: ID})).data;
  assert.equal(result.report.cycle.baseline_verified, true);
  assert.equal(result.report.cycle.daily_pnl_pct, 0);
  assert.deepEqual(result.report.cycle.records[0].gate.codes, ['R11_NO_APPROVAL']);
  assert.equal(result.approvals.tickets[0].eligible, true);
  assert.equal(result.report.cycle.historical, false);
  assert.deepEqual(await w.store.approvals('owner', ID), []);
  assert.equal(result.trading_enabled, false);
});

test('opening baseline selection uses ET day and never carries into the next session', async t => {
  const w = await baselineWorkspace(t, () => Date.parse('2026-11-21T01:00:00Z'));
  await w.call('scout-import-opening', openingBody());
  assert.equal((await w.call('scout-status')).data.baselines.current_day, '2026-11-20');
  assert.ok(await w.f.baselines.forTime('owner', Date.parse('2026-11-21T01:00:00Z')));
  assert.equal(await w.f.baselines.forTime('owner', Date.parse('2026-11-23T15:00:00Z')), null);
  assert.equal(await w.f.baselines.forTime('other', NOW), null);
});

test('importing evidence leaves old report untouched until explicit historical re-evaluation', async t => {
  let time = NOW;
  const w = await baselineWorkspace(t, () => time);
  const payload = artifact(initial, NOW);
  payload.market_raw.symbols['US.SPY'].market_cap = 1e12;
  w.f.cloud.collect = async () => { w.f.counts.collects++; return {status: 'completed', payload}; };
  await w.call('scout-start', {request_id: ID, version: 0});
  const first = (await w.call('scout-sync', {request_id: ID})).data;
  time += 86400000;
  const imported = (await w.call('scout-import-opening', openingBody())).data;
  assert.deepEqual(imported.report, first.report);
  assert.equal(imported.version, first.version);
  assert.equal(imported.baselines.current, null);
  assert.equal(imported.baselines.reassessment_available, true);
  await w.call('scout-status');
  assert.equal(w.f.counts.collects, 1);
  const revised = (await w.call('scout-sync', {request_id: ID})).data;
  assert.equal(revised.report.cycle.baseline_verified, true);
  assert.equal(revised.report.cycle.historical, true);
  assert.equal(revised.report.cycle.generated_at, first.report.cycle.generated_at);
  assert.equal(Date.parse(revised.report.cycle.evaluated_at), time);
  assert.equal(revised.report.as_of, first.report.as_of);
  assert.equal(revised.completed_at, first.completed_at);
  assert.equal(revised.approvals.tickets[0].eligible, false);
  assert.equal(revised.approvals.tickets[0].reason, 'historical_assessment');
  assert.deepEqual(await w.store.approvals('owner', ID), []);
  await w.call('scout-sync', {request_id: ID});
  assert.deepEqual(w.f.counts, {creates: 1, sends: 1, collects: 2});
});

test('opening re-evaluation retains prior report on changed artifact or evaluator failure', async t => {
  const w = await baselineWorkspace(t), payload = artifact(initial, NOW);
  w.f.cloud.collect = async () => ({status: 'completed', payload});
  await w.call('scout-start', {request_id: ID, version: 0});
  const before = (await w.call('scout-sync', {request_id: ID})).data;
  await w.call('scout-import-opening', openingBody());
  payload.cash.data.balance++;
  assert.equal((await w.call('scout-sync', {request_id: ID})).data.error, 'invalid_scout_result');
  payload.cash.data.balance--;
  w.f.evaluateTickets = ticketEvaluator(join(w.root, 'missing-python'));
  assert.equal((await client(w.store, w.f)('scout-sync', {request_id: ID})).data.error, 'ticket_generation_unavailable');
  assert.deepEqual((await w.store.read('owner')).last_report, before.report);
  assert.equal((await w.store.read('owner')).status, 'completed');
});

test('opening import HTTP is local-only, origin protected and bounded', async t => {
  const w = await workspace(t), f = fixture({now: () => NOW});
  const {server, url} = await startScoutServer({databasePath: w.path, webDirectory: await web(w.root), cloud: f.cloud, port: 0, now: () => NOW});
  w.cleanup.push(() => stop(server));
  assert.equal((await httpCall(url, 'scout-import-opening', openingBody(), {Origin: 'https://untrusted.test'})).status, 403);
  assert.equal((await httpCall(url, 'scout-import-opening')).status, 405);
  assert.equal((await httpCall(url, 'scout-import-opening', {source_text: 'x'.repeat(65536), confirmed: true})).status, 413);
  assert.equal((await httpCall(url, 'scout-import-opening', openingBody())).status, 200);
  assert.equal((await fetch(url + '/opening-record.json')).status, 404);
  const store = sqliteScoutStore(w.path);
  try {
    assert.equal((await client(store, f)('scout-import-opening', openingBody())).status, 404);
    assert.ok(await store.baseline('local-owner', '2026-11-20'));
  } finally { store.close(); }
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});

test('version-two migration preserves approvals while adding empty baseline storage', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path);
  await store.insert({...initial, status: 'completed'});
  await store.saveApprovals('owner', 1, ID, [{ticket_id: 'test', proposal_hash: 'a'.repeat(64), approved_at: '2026-11-20T15:00:00Z', expires_at: '2026-11-21T03:00:00Z'}]);
  store.close();
  const db = new DatabaseSync(w.path);
  db.exec('DROP TABLE scout_baselines; PRAGMA user_version = 2;'); db.close();
  const migrated = sqliteScoutStore(w.path);
  try {
    assert.equal((await migrated.read('owner')).version, 2);
    assert.equal((await migrated.approvals('owner', ID)).length, 1);
    assert.equal(await migrated.baseline('owner', '2026-11-20'), null);
  } finally { migrated.close(); }
});

function approvalBody(report, indexes = [0, 1], version = 1) {
  return {request_id: report.request_id, version, tickets: indexes.map(i => ({ticket_id: report.cycle.records[i].ticket_id, proposal_hash: report.cycle.records[i].proposal_hash}))};
}
async function approvalWorkspace(t, {now = () => NOW, mutate = () => {}} = {}) {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now});
  w.cleanup.push(() => store.close());
  const report = approvalFixtureReport(initial, NOW);
  mutate(report);
  await store.insert({...initial, status: 'completed', completed_at: new Date(NOW).toISOString(), last_report: report});
  f.approvals = scoutApprovals({store, now});
  return {...w, store, f, report, call: client(store, f)};
}

test('version-one SQLite migration preserves the existing run and adds isolated approval storage', async t => {
  const w = await workspace(t), db = new DatabaseSync(w.path);
  db.exec(`CREATE TABLE scout_state (owner_id TEXT PRIMARY KEY, version INTEGER, request_id TEXT, started_at TEXT, status TEXT, session_id TEXT, event_ids TEXT, completed_at TEXT, error_code TEXT, last_report TEXT);
    PRAGMA application_id = 1396920148; PRAGMA user_version = 1;`);
  db.prepare('INSERT INTO scout_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)').run(...Object.values({...initial, event_ids: '[]', last_report: 'null'}));
  db.close();
  const store = sqliteScoutStore(w.path);
  w.cleanup.push(() => store.close());
  assert.deepEqual(await store.read('owner'), initial);
  assert.deepEqual(await store.approvals('owner', ID), []);
  const check = new DatabaseSync(w.path, {readOnly: true});
  try { assert.equal(check.prepare('PRAGMA user_version').get().user_version, 3); } finally { check.close(); }
});

test('batch approvals are durable, hash-bound, idempotent and never dispatch or change gate results', async t => {
  let time = NOW;
  const w = await approvalWorkspace(t, {now: () => time});
  assert.equal((await w.call('scout-status')).data.approvals.enabled, true);
  const body = approvalBody(w.report);
  const result = await w.call('scout-approve', body);
  assert.equal(result.status, 200);
  assert.equal(result.data.version, 2);
  assert.equal(result.data.trading_enabled, false);
  assert.deepEqual(result.data.report, w.report);
  assert.equal(result.data.approvals.tickets.filter(item => item.status === 'APPROVED').length, 2);
  const saved = await w.store.approvals('owner', ID);
  assert.equal(Date.parse(saved[0].expires_at) - Date.parse(saved[0].approved_at), 12 * 3600000);
  time += 60000;
  assert.equal((await w.call('scout-approve', body)).status, 200);
  assert.deepEqual(await w.store.approvals('owner', ID), saved);
  const reopened = sqliteScoutStore(w.path);
  try { assert.deepEqual(await reopened.approvals('owner', ID), saved); } finally { reopened.close(); }
  assert.deepEqual(await w.store.approvals('other', ID), []);
  assert.deepEqual(w.f.counts, {creates: 0, sends: 0, collects: 0});
});

test('a rejected ticket blocks the whole approval batch without partial writes', async t => {
  const w = await approvalWorkspace(t);
  assert.equal((await w.call('scout-approve', approvalBody(w.report, [0, 2]))).data.error, 'ticket_not_eligible');
  assert.equal((await w.store.read('owner')).version, 1);
  assert.deepEqual(await w.store.approvals('owner', ID), []);
});

test('approval checks reject forged status, missing gates, unverified baseline and historical assessments', async t => {
  for (const [mutate, error] of [
    [r => { r.cycle.records[0].status = 'READY'; }, 'ticket_not_eligible'],
    [r => { r.cycle.records[0].gate.codes.push('R4_FUNDS'); }, 'ticket_not_eligible'],
    [r => { r.cycle.records[0].gate.checked.pop(); }, 'ticket_not_eligible'],
    [r => { r.cycle.baseline_verified = false; }, 'ticket_not_eligible'],
    [r => { r.cycle.historical = true; }, 'historical_assessment'],
    [r => { r.cycle.request_id = NEXT; }, 'report_not_current'],
    [r => { r.cycle.generated_at = new Date(NOW + 60000).toISOString(); }, 'ticket_review_expired'],
  ]) {
    const w = await approvalWorkspace(t, {mutate});
    assert.equal((await w.call('scout-approve', approvalBody(w.report, [0]))).data.error, error);
    assert.deepEqual(await w.store.approvals('owner', ID), []);
  }
});

test('stale versions, changed hashes, malformed batches and foreign requests cannot approve', async t => {
  const w = await approvalWorkspace(t), valid = approvalBody(w.report, [0]);
  for (const [body, error] of [
    [{...valid, version: 2}, 'version_conflict'],
    [{...valid, request_id: NEXT}, 'not_found'],
    [{...valid, tickets: [{...valid.tickets[0], proposal_hash: 'f'.repeat(64)}]}, 'ticket_changed'],
    [{...valid, tickets: [{...valid.tickets[0], ticket_id: 'missing'}]}, 'ticket_changed'],
    [{...valid, tickets: [valid.tickets[0], valid.tickets[0]]}, 'invalid_input'],
    [{...valid, tickets: []}, 'invalid_input'],
    [{...valid, tickets: Array(22).fill(valid.tickets[0])}, 'invalid_input'],
    [{...valid, tickets: [null]}, 'invalid_input'],
    [{...valid, tickets: [{...valid.tickets[0], approved_at: new Date(NOW).toISOString()}]}, 'invalid_input'],
    [{...valid, ttl_minutes: 10000}, 'invalid_input'],
    [{...valid, trading_enabled: true}, 'invalid_input'],
  ]) assert.equal((await w.call('scout-approve', body)).data.error, error);
  assert.deepEqual(await w.store.approvals('owner', ID), []);
  assert.equal((await w.call('scout-approve')).status, 405);
});

test('approval expiry is visible without renewing consent or reusing an expired ticket', async t => {
  let time = NOW;
  const w = await approvalWorkspace(t, {now: () => time});
  await w.call('scout-approve', approvalBody(w.report, [0]));
  const saved = await w.store.approvals('owner', ID);
  time += 12 * 3600000;
  const state = (await w.call('scout-status')).data;
  assert.equal(state.approvals.tickets[0].status, 'EXPIRED');
  assert.equal(state.approvals.tickets[0].eligible, false);
  assert.equal((await w.call('scout-approve', approvalBody(w.report, [0], state.version))).data.error, 'ticket_review_expired');
  assert.equal((await w.call('scout-approve', approvalBody(w.report, [1], state.version))).data.error, 'ticket_review_expired');
  assert.deepEqual(await w.store.approvals('owner', ID), saved);
});

test('approval records survive newer Scouts but never transfer to them', async t => {
  const w = await approvalWorkspace(t);
  await w.call('scout-approve', approvalBody(w.report, [0]));
  const approved = await w.store.approvals('owner', ID);
  await w.store.change('owner', 2, {request_id: NEXT, status: 'running'});
  assert.equal((await w.call('scout-status')).data.approvals.tickets[1].eligible, false);
  assert.equal((await w.call('scout-approve', {...approvalBody(w.report, [1], 3), request_id: NEXT})).data.error, 'report_not_current');
  await w.store.change('owner', 3, {status: 'completed', last_report: approvalFixtureReport({...initial, request_id: NEXT}, NOW)});
  const state = (await w.call('scout-status')).data;
  assert.ok(state.approvals.tickets.every(item => !item.approval));
  assert.deepEqual(await w.store.approvals('owner', ID), approved);
});

test('concurrent approval batches cannot double-record or overwrite state', async t => {
  const w = await approvalWorkspace(t), other = sqliteScoutStore(w.path);
  w.cleanup.push(() => other.close());
  const call = client(other, {...w.f, approvals: scoutApprovals({store: other, now: () => NOW})});
  const results = await Promise.all([w.call('scout-approve', approvalBody(w.report)), call('scout-approve', approvalBody(w.report))]);
  assert.ok(results.some(result => result.status === 200));
  assert.ok(results.every(result => [200, 409].includes(result.status)));
  assert.equal((await w.store.approvals('owner', ID)).length, 2);
  assert.equal((await w.store.read('owner')).version, 2);
});

test('approval transaction rolls back its entire batch and state version on storage failure', async t => {
  const w = await approvalWorkspace(t);
  const ticket = approvalBody(w.report, [0]).tickets[0];
  const item = {...ticket, approved_at: new Date(NOW).toISOString(), expires_at: new Date(NOW + 3600000).toISOString()};
  await assert.rejects(w.store.saveApprovals('owner', 1, ID, [item, item]), {code: 'scout_storage_unavailable'});
  assert.deepEqual(await w.store.approvals('owner', ID), []);
  assert.equal((await w.store.read('owner')).version, 1);
});

test('local HTTP approval is same-origin only, supports a batch, and exposes no execution endpoint', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => NOW});
  const report = approvalFixtureReport(initial, NOW);
  report.cycle.records = Array.from({length: 21}, (_, i) => ({...report.cycle.records[0], ticket_id: `P-SYNTHETIC-${i}`}));
  await store.insert({...initial, owner_id: 'local-owner', status: 'completed', last_report: report});
  store.close();
  const directory = await web(w.root);
  const {server, url} = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud: f.cloud, now: () => NOW, port: 0});
  w.cleanup.push(() => stop(server));
  const body = approvalBody(report, Array.from({length: 21}, (_, i) => i));
  assert.ok(JSON.stringify(body).length > 1024);
  assert.equal((await httpCall(url, 'scout-approve', body, {Origin: 'https://untrusted.test'})).status, 403);
  assert.equal((await fetch(`${url}/functions/v1/app?action=scout-approve`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})).status, 403);
  const done = await (await httpCall(url, 'scout-approve', body)).json();
  assert.equal(done.approvals.tickets.filter(item => item.status === 'APPROVED').length, 21);
  assert.equal(done.trading_enabled, false);
  for (const action of ['guard', 'execute', 'place-order', 'clear-halt']) assert.equal((await httpCall(url, action, {request_id: ID})).status, 404);
  assert.equal((await httpCall(url, 'scout-approve', {padding: 'x'.repeat(9000)})).status, 413);
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});

test('hosted research-only application does not expose approval recording', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture();
  w.cleanup.push(() => store.close());
  const call = client(store, f);
  assert.equal((await call('scout-status')).data.approvals.enabled, false);
  assert.equal((await call('scout-approve', {request_id: ID})).status, 404);
});

test('token loading is opt-in and a nonempty inherited token takes precedence', async t => {
  const w = await workspace(t), missing = join(w.root, 'missing.env');
  assert.equal(await loadScoutToken(undefined, {}), '');
  assert.equal(await loadScoutToken(undefined, {QODER_PAT_TKN: '  '}), '');
  assert.equal(await loadScoutToken(missing, {QODER_PAT_TKN: ' dummy-inherited '}), 'dummy-inherited');
  await assert.rejects(loadScoutToken(missing, {}), {message: 'Local Qoder credentials could not be loaded.'});
});

test('explicit dotenv loading supports quotes, comments and CRLF without importing other keys', async t => {
  const w = await workspace(t), path = join(w.root, 'selected.env');
  const environment = {QODER_PAT_TKN: ' ', KEEP: 'unchanged'};
  await writeFile(path, '# fixture only\r\nexport QODER_PAT_TKN="dummy-file-token" # chosen\r\nKEEP=changed\r\nNODE_OPTIONS=--require ./must-not-run.cjs\r\n');
  assert.equal(await loadScoutToken(path, environment), 'dummy-file-token');
  assert.deepEqual(environment, {QODER_PAT_TKN: ' ', KEEP: 'unchanged'});
  await writeFile(path, "QODER_PAT_TKN='dummy-$UNSET-$(not-a-command)'\n");
  assert.equal(await loadScoutToken(path, {}), 'dummy-$UNSET-$(not-a-command)');
});

test('invalid selected credentials produce only a fixed safe error', async t => {
  const w = await workspace(t), path = join(w.root, 'selected.env');
  for (const source of ['', 'OTHER=dummy-private-value\n', 'QODER_PAT_TKN=\n', 'QODER_PAT_TKN="  "\n',
    'QODER_PAT_TKN="dummy token"\n', 'QODER_PAT_TKN="dummy\nnewline"\n', 'QODER_PAT_TKN="dummy\u0001control"\n', 'QODER_PAT_TKN="dummy-\u00e9"\n']) {
    await writeFile(path, source);
    await assert.rejects(loadScoutToken(path, {}), error => {
      assert.equal(error.message, 'Local Qoder credentials could not be loaded.');
      assert.equal(error.cause, undefined);
      assert.doesNotMatch(error.stack, /dummy-private-value|selected\.env/);
      return true;
    });
  }
  await assert.rejects(loadScoutToken(w.root, {}), {message: 'Local Qoder credentials could not be loaded.'});
  await assert.rejects(loadScoutToken(undefined, {QODER_PAT_TKN: 'dummy\r\nInjected: value'}), {message: 'Local Qoder credentials could not be loaded.'});
});

test('dotenv input is bounded and must be valid UTF-8', async t => {
  const w = await workspace(t), path = join(w.root, 'selected.env');
  const prefix = 'QODER_PAT_TKN=dummy-bounded\n#';
  await writeFile(path, prefix + 'a'.repeat(65536 - Buffer.byteLength(prefix)));
  assert.equal(await loadScoutToken(path, {}), 'dummy-bounded');
  await writeFile(path, prefix + 'a'.repeat(65537 - Buffer.byteLength(prefix)));
  await assert.rejects(loadScoutToken(path, {}), {message: 'Local Qoder credentials could not be loaded.'});
  await writeFile(path, Buffer.concat([Buffer.from('QODER_PAT_TKN=dummy\n#'), Buffer.from([0xff])]));
  await assert.rejects(loadScoutToken(path, {}), {message: 'Local Qoder credentials could not be loaded.'});
});

test('file-loaded token configures local status without disclosure, persistence or cloud calls', async t => {
  const w = await workspace(t), directory = await web(w.root), path = join(w.root, 'selected.env');
  const dummy = 'dummy-server-only-token';
  await writeFile(path, `QODER_PAT_TKN=${dummy}\n`);
  const token = await loadScoutToken(path, {});
  let calls = 0;
  const cloud = scoutCloud({getToken: () => token, fetcher: async () => { calls++; throw new Error('No external calls allowed'); }});
  const {server, url} = await startScoutServer({databasePath: w.path, webDirectory: directory, cloud, port: 0});
  w.cleanup.push(() => stop(server));
  const response = await httpCall(url, 'scout-status');
  const source = await response.text(), status = JSON.parse(source);
  assert.equal(response.status, 200);
  assert.equal(status.configured, true);
  assert.equal(status.trading_enabled, false);
  assert.equal(source.includes(dummy), false);
  assert.equal((await (await fetch(url)).text()).includes(dummy), false);
  assert.equal((await fetch(`${url}/selected.env`)).status, 404);
  const database = new DatabaseSync(w.path);
  try { assert.equal(database.prepare('SELECT count(*) AS n FROM scout_state').get().n, 0); }
  finally { database.close(); }
  assert.equal(calls, 0);
});

test('CLI help skips credential loading and invalid files fail before creating a database', async t => {
  const w = await workspace(t), script = fileURLToPath(new URL('../dashboard/scout-server.mjs', import.meta.url));
  const options = {cwd: w.root, env: {QODER_PAT_TKN: ''}, timeout: 10000};
  const run = promisify(execFile), missing = join(w.root, 'missing.env');
  const help = await run(process.execPath, [script, '--scout-env-file', missing, '--help'], options);
  assert.match(help.stdout, /--scout-env-file/);
  assert.match(help.stdout, /No file is loaded by default/);
  await assert.rejects(run(process.execPath, [script, '--scout-env-file', missing, '--db', w.path], options), error => {
    assert.equal(error.code, 1);
    assert.equal(error.stdout, '');
    assert.match(error.stderr, /Local Scout startup failed/);
    assert.doesNotMatch(error.stderr, /missing\.env|ENOENT/);
    return true;
  });
  await assert.rejects(readFile(w.path), {code: 'ENOENT'});
});

test('CLI starts with its scoped dotenv flag without Node importing other file options', async t => {
  const w = await workspace(t), directory = await web(w.root), path = join(w.root, 'selected.env');
  await writeFile(path, 'QODER_PAT_TKN=dummy-cli-only\nNODE_OPTIONS=--not-a-valid-node-option\n');
  const reservation = createServer();
  await new Promise((accept, reject) => { reservation.once('error', reject); reservation.listen(0, '127.0.0.1', accept); });
  const port = reservation.address().port;
  await stop(reservation);
  const script = fileURLToPath(new URL('../dashboard/scout-server.mjs', import.meta.url));
  const child = spawn(process.execPath, [script, '--scout-env-file', path, '--db', w.path, '--web', directory, '--port', String(port)], {cwd: w.root, env: {QODER_PAT_TKN: ''}, timeout: 10000, stdio: ['ignore', 'pipe', 'pipe']});
  const closed = new Promise(accept => child.once('close', accept));
  w.cleanup.push(async () => { if (child.exitCode === null && child.signalCode === null) child.kill(); await closed; });
  let output = '', errors = '';
  child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { errors += chunk; });
  const started = await new Promise((accept, reject) => {
    child.once('error', reject);
    child.once('close', () => reject(new Error('Fixture server exited before startup.')));
    child.stdout.on('data', chunk => {
      output += chunk;
      if (output.includes('\n')) {
        try { accept(JSON.parse(output.split('\n')[0])); } catch (error) { reject(error); }
      }
    });
  });
  assert.equal(started.configured, true);
  assert.equal(started.trading_enabled, false);
  assert.equal(started.url, `http://127.0.0.1:${port}`);
  const status = await (await httpCall(started.url, 'scout-status')).text();
  assert.equal(JSON.parse(status).configured, true);
  assert.equal((output + errors + status).includes('dummy-cli-only'), false);
});

test('missing server-side Qoder access never allocates a run or loses saved state', async t => {
  const w = await workspace(t), store = sqliteScoutStore(w.path), f = fixture({now: () => NOW});
  w.cleanup.push(() => store.close());
  f.cloud.configured = () => false;
  const call = client(store, f);
  assert.equal((await call('scout-status')).data.configured, false);
  assert.equal((await call('scout-start', {request_id: ID, version: 0})).data.error, 'scout_not_configured');
  assert.equal(await store.read('owner'), null);
  assert.deepEqual(f.counts, {creates: 0, sends: 0, collects: 0});
});
