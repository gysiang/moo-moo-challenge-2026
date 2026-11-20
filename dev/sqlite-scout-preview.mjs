import {fileURLToPath} from 'node:url';
import {mkdtemp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {parseArgs} from 'node:util';
import {startScoutServer} from '../dashboard/scout-server.mjs';
import {sqliteScoutStore} from '../dashboard/scout-store.mjs';
import {scoutReport} from '../functions/scout-report.mjs';
import {artifact, fixture} from './hosted-scout-fixture.mjs';

const {values} = parseArgs({options: {scenario: {type: 'string', default: 'new'}, port: {type: 'string', default: '8768'}}});
if (!['new', 'legacy', 'empty', 'error'].includes(values.scenario)) throw new Error('Unknown preview scenario');
const stamp = Date.parse(values.scenario === 'empty' ? '2026-11-21T15:00:00Z' : '2026-11-20T15:00:00Z');
const now = () => stamp;
const {cloud, options, counts} = fixture({now});
const databasePath = join(await mkdtemp(join(tmpdir(), 'scout-ticket-preview-')), 'scout.sqlite3');
options.closed = values.scenario === 'empty';
if (values.scenario === 'legacy') {
  const run = {owner_id: 'local-owner', version: 4, request_id: 'a'.repeat(32), started_at: new Date(stamp).toISOString(), status: 'completed', session_id: 'sess_fixture', event_ids: ['evt_fixture'], completed_at: new Date(stamp).toISOString(), error_code: null};
  const store = sqliteScoutStore(databasePath);
  await store.insert({...run, last_report: scoutReport(artifact(run, stamp), run, stamp)});
  store.close();
}
for (const key of ['create', 'send', 'collect']) {
  const action = cloud[key];
  cloud[key] = async (...args) => { const result = await action(...args); console.log(JSON.stringify({synthetic: true, counts})); return result; };
}
const {server, url} = await startScoutServer({
  databasePath,
  webDirectory: fileURLToPath(new URL('./hosted-web/', import.meta.url)),
  cloud, now, port: Number(values.port), synthetic: true,
  python: values.scenario === 'error' ? join(tmpdir(), 'scout-missing-python') : 'python',
});
console.log(JSON.stringify({url, scenario: values.scenario, synthetic: true, storage: 'SQLite', trading_enabled: false}));
process.once('SIGINT', () => server.close());
process.once('SIGTERM', () => server.close());
