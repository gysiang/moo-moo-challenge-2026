import http from 'node:http';
import {open, readFile, realpath} from 'node:fs/promises';
import {dirname, isAbsolute, relative, resolve} from 'node:path';
import {parseArgs, parseEnv} from 'node:util';
import {sqliteScoutStore} from './scout-store.mjs';
import {scoutApplication} from '../functions/scout-routes.mjs';
import {scoutCloud} from '../functions/scout-cloud.mjs';
import {ticketEvaluator} from './scout-tickets.mjs';
import {scoutApprovals} from './scout-approvals.mjs';
import {scoutBaselines} from './scout-baselines.mjs';

const TYPES = {'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'hosted-scout.js': 'text/javascript', 'style.css': 'text/css', 'state.json': 'application/json', 'cloud.json': 'application/json'};
const inside = (root, path) => { const part = relative(root, path); return !part.startsWith('..') && !isAbsolute(part); };

export async function loadScoutToken(envFile, environment = process.env) {
  try {
    let token = environment.QODER_PAT_TKN?.trim();
    if (!token && envFile) {
      const file = await open(envFile, 'r');
      try {
        if (!(await file.stat()).isFile()) throw new Error();
        const buffer = Buffer.alloc(65537);
        let length = 0;
        while (length < buffer.length) {
          const {bytesRead} = await file.read(buffer, length, buffer.length - length, null);
          if (!bytesRead) break;
          length += bytesRead;
        }
        if (length > 65536) throw new Error();
        const source = new TextDecoder('utf-8', {fatal: true}).decode(buffer.subarray(0, length));
        token = parseEnv(source).QODER_PAT_TKN?.trim();
        if (!token) throw new Error();
      } finally { await file.close(); }
    }
    if (token && !/^[\x21-\x7e]+$/.test(token)) throw new Error();
    return token || '';
  } catch { throw new Error('Local Qoder credentials could not be loaded.'); }
}

export async function startScoutServer({databasePath, webDirectory, cloud, port = 8766, synthetic = false, python = 'python', now = Date.now}) {
  const root = await realpath(webDirectory);
  const dbPath = resolve(databasePath);
  const dbParent = await realpath(dirname(dbPath));
  if (inside(root, dbPath) || inside(root, dbParent)) throw new Error('The database must be outside the served directory.');
  const pages = new Map();
  for (const name of Object.keys(TYPES)) {
    const path = await realpath(resolve(root, name));
    if (!inside(root, path)) throw new Error('Assets must stay inside the served directory.');
    pages.set(name, await readFile(path));
  }
  let html = pages.get('index.html').toString('utf8');
  if (!html.includes('data-static="1" data-hosted-scout="1"')) throw new Error('Export the Scout dashboard before starting the server.');
  html = html.replace('data-hosted-scout="1"', 'data-hosted-scout="1" data-local-scout="1"');
  pages.set('index.html', Buffer.from(html));
  const store = sqliteScoutStore(dbPath);
  const app = scoutApplication({store, cloud, now, evaluateTickets: ticketEvaluator(python), approvals: scoutApprovals({store, now}), baselines: scoutBaselines({store, now, python}), requireUser: () => ({id: 'local-owner'})});
  const server = http.createServer(async (req, res) => {
    const origin = `http://127.0.0.1:${server.address().port}`;
    const send = (status, body, type = 'application/json') => {
      res.writeHead(status, {'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'});
      res.end(body);
    };
    const fail = (status, error) => send(status, JSON.stringify({ok: false, error}));
    if (req.headers.host !== new URL(origin).host || req.headers['sec-fetch-site'] === 'cross-site' ||
        (req.headers.origin && req.headers.origin !== origin) || (req.method === 'POST' && req.headers.origin !== origin)) {
      req.resume(); fail(403, 'invalid_user_context'); return;
    }
    if (!['GET', 'POST'].includes(req.method)) { req.resume(); fail(405, 'method_not_allowed'); return; }
    try {
      const url = new URL(req.url, origin);
      if (url.origin !== origin) { fail(403, 'invalid_user_context'); return; }
      if (url.pathname === '/functions/v1/app') {
        const chunks = [];
        let size = 0;
        for await (const chunk of req.iterator({destroyOnReturn: false})) {
          size += chunk.length;
          if (size > (url.searchParams.get('action') === 'scout-import-opening' ? 65536 : url.searchParams.get('action') === 'scout-approve' ? 8192 : 1024)) { req.resume(); fail(413, 'invalid_input'); return; }
          chunks.push(chunk);
        }
        if (req.method === 'GET' && size) { fail(400, 'invalid_input'); return; }
        const request = new Request(url, {method: req.method, headers: req.headers, ...(size ? {body: Buffer.concat(chunks)} : {})});
        const output = await app({request});
        const payload = await output.json();
        if (synthetic) payload.fixture = true;
        send(output.status, JSON.stringify(payload)); return;
      }
      req.resume();
      const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (req.method !== 'GET' || !pages.has(name)) { fail(404, 'not_found'); return; }
      send(200, pages.get(name), TYPES[name]);
    } catch { fail(503, 'scout_storage_unavailable'); }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.maxHeadersCount = 64;
  server.once('close', () => store.close());
  try {
    await new Promise((accept, reject) => { server.once('error', reject); server.listen(port, '127.0.0.1', accept); });
  } catch (error) { store.close(); throw error; }
  return {server, url: `http://127.0.0.1:${server.address().port}`};
}

if (import.meta.main) {
  try {
    const {values} = parseArgs({options: {db: {type: 'string', default: 'runtime/scout-local.sqlite3'}, web: {type: 'string', default: 'runtime/scout-web'}, port: {type: 'string', default: '8766'}, 'scout-env-file': {type: 'string'}, python: {type: 'string', default: 'python'}, help: {type: 'boolean'}}});
    if (values.help) {
      console.log('node dashboard/scout-server.mjs [--db runtime/scout-local.sqlite3] [--web runtime/scout-web] [--port 8766] [--scout-env-file .env] [--python python]\nTicket review uses the existing Python Scout/gates; Python 3.10+ with project dependencies is required. Opening records can be explicitly imported on the dashboard as operator-attested, immutable JSON evidence for the matching 09:30 ET open; no current-balance substitution. Approvals are recorded only in SQLite; operational approval files and journals are untouched. No Guard calls or orders are sent.\nExport first: python -m dashboard.export --hosted-scout --no-cloud --out runtime/scout-web\nQoder access uses QODER_PAT_TKN from the process environment first, then the explicitly selected --scout-env-file. No file is loaded by default.\nOnly QODER_PAT_TKN is used; other file values are not imported or executed. Restart after changing credentials.\nLoopback-only, single local operator. Trading and Guard execution are disabled.');
    } else {
      const port = Number(values.port);
      if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Use a port between 1024 and 65535.');
      const token = await loadScoutToken(values['scout-env-file']);
      const cloud = scoutCloud({getToken: () => token});
      const {server, url} = await startScoutServer({databasePath: values.db, webDirectory: values.web, cloud, port, python: values.python});
      console.log(JSON.stringify({url, storage: 'SQLite', configured: cloud.configured(), trading_enabled: false}));
      const stop = () => server.close();
      process.once('SIGINT', stop);
      process.once('SIGTERM', stop);
    }
  } catch { console.error('Local Scout startup failed. Check the export, database path, Node SQLite support, port and any explicit environment file; no Scout was started.'); process.exitCode = 1; }
}
