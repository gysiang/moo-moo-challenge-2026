import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {scoutApplication} from '../functions/scout-routes.mjs';
import {fixture} from './hosted-scout-fixture.mjs';

const f = fixture(), app = scoutApplication(f);
const root = new URL('./hosted-web/', import.meta.url);
const types = {'index.html': 'text/html', 'app.js': 'text/javascript', 'hosted-scout.js': 'text/javascript', 'style.css': 'text/css', 'state.json': 'application/json', 'cloud.json': 'application/json'};
const server = http.createServer(async (req, res) => {
  const origin = `http://127.0.0.1:${server.address().port}`;
  if (req.headers.host !== new URL(origin).host || (req.method === 'POST' && req.headers.origin !== origin)) {res.writeHead(403); res.end(); return;}
  const url = new URL(req.url, origin);
  try {
    if (url.pathname === '/fixture/status') {
      res.writeHead(200, {'Content-Type': 'application/json'}); res.end(JSON.stringify({counts: f.counts, options: f.options})); return;
    }
    if (url.pathname === '/fixture/setup' && req.method === 'POST') {
      let body = '';
      for await (const chunk of req) {body += chunk; if (body.length > 1024) throw new Error('large body');}
      const input = JSON.parse(body);
      for (const key of Object.keys(f.options)) f.options[key] = input[key] === true;
      if (input.reset === true) f.saved.clear();
      res.writeHead(200, {'Content-Type': 'application/json'}); res.end('{"ok":true}'); return;
    }
    if (url.pathname === '/functions/v1/app') {
      const chunks = [];
      let bytes = 0;
      for await (const chunk of req) { bytes += chunk.length; if (bytes > 2048) throw new Error('large body'); chunks.push(chunk); }
      const request = new Request(url, {method: req.method, headers: req.headers, ...(chunks.length ? {body: Buffer.concat(chunks)} : {})});
      const output = await app({request, user: {id: 'local-fixture-owner'}});
      const payload = await output.json(); payload.fixture = true;
      res.writeHead(output.status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
      res.end(JSON.stringify(payload)); return;
    }
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if (req.method !== 'GET' || !Object.hasOwn(types, name)) {res.writeHead(404); res.end(); return;}
    const body = await readFile(new URL(name, root));
    res.writeHead(200, {'Content-Type': types[name], 'Cache-Control': 'no-store'}); res.end(body);
  } catch {res.writeHead(500, {'Content-Type': 'application/json'}); res.end('{"ok":false,"error":"fixture_error"}');}
});
server.listen(8769, '127.0.0.1', () => console.log(JSON.stringify({url: `http://127.0.0.1:${server.address().port}`, pid: process.pid, directory: fileURLToPath(root), fixture: true})));
