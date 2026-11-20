import { ScoutError } from './scout-report.mjs';

const ORIGIN = 'https://api.qoder.com';
const IDENTITY = 'idn_4a6682c9f973d0ba24f3ffa2';
const TEMPLATE = 'tmpl_834e2027a979a145b432ff03';
const sessionId = value => typeof value === 'string' && /^sess_[A-Za-z0-9]+$/.test(value);

export async function boundedJson(response, limit = 2000000) {
  if (!response.body) throw new ScoutError('invalid_cloud_response');
  const reader = response.body.getReader(), chunks = [];
  let length = 0;
  try {
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.length;
      if (length > limit) throw new ScoutError('response_too_large');
      chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  try { return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes)); }
  catch { throw new ScoutError('invalid_cloud_response'); }
}

export function scoutCloud({getToken, fetcher = fetch}) {
  async function request(path, body) {
    const token = getToken();
    if (!token) throw new ScoutError('scout_not_configured');
    let response;
    try {
      response = await fetcher(ORIGIN + path, {method: body ? 'POST' : 'GET', redirect: 'error', signal: AbortSignal.timeout(20000),
        headers: {'Authorization': `Bearer ${token}`, 'Accept': 'application/json', ...(body ? {'Content-Type': 'application/json'} : {})},
        ...(body ? {body: JSON.stringify(body)} : {})});
    } catch { throw new ScoutError(body ? 'dispatch_unknown' : 'cloud_unavailable'); }
    if (!response.ok) {
      await response.body?.cancel();
      throw new ScoutError(body && response.status >= 400 && response.status < 500 ? 'dispatch_rejected' : body ? 'dispatch_unknown' : 'cloud_unavailable');
    }
    return boundedJson(response);
  }
  return {
    configured: () => Boolean(getToken()),
    async create(run) {
      const payload = await request('/api/v1/forward/sessions', {identity_id: IDENTITY, template_id: TEMPLATE, title: `Dashboard scout ${run.request_id}`});
      const session = payload?.item ?? payload;
      if (!sessionId(session?.id)) throw new ScoutError('dispatch_unknown');
      return session.id;
    },
    async send(run) {
      if (!sessionId(run.session_id)) throw new ScoutError('dispatch_unknown');
      const text = JSON.stringify({kind: 'dashboard-request', version: 1, mode: 'scout', request_id: run.request_id, account_id: '5829804', market: 100, requested_at: run.started_at});
      const response = await request(`/api/v1/forward/sessions/${run.session_id}/events`, {events: [{type: 'user.message', content: [{type: 'text', text}]}]});
      if (!Array.isArray(response?.data) || !response.data.length || response.data.some(event => typeof event?.id !== 'string' || !event.id)) throw new ScoutError('dispatch_unknown');
      return response.data.map(event => event.id);
    },
    async collect(run) {
      if (!sessionId(run.session_id)) return {status: 'unknown'};
      const name = `dashboard-${run.request_id}.json`;
      const query = new URLSearchParams({scope_id: run.session_id, name, limit: '100', order: 'desc'});
      const payload = await request(`/api/v1/cloud/files?${query}`);
      if (!Array.isArray(payload?.data) || payload.has_more === true) throw new ScoutError('invalid_cloud_response');
      const matches = payload.data.filter(file => file.filename === name && file.scope?.id === run.session_id);
      if (matches.length > 1) throw new ScoutError('invalid_scout_result', 422);
      if (matches.length) {
        const id = matches[0].id;
        if (typeof id !== 'string' || !/^file_[A-Za-z0-9]+$/.test(id)) throw new ScoutError('invalid_cloud_response');
        const content = await request(`/api/v1/cloud/files/${id}/content`);
        let url;
        try { url = new URL(content.url); } catch { throw new ScoutError('invalid_cloud_response'); }
        if (url.protocol !== 'https:' || url.username || url.password || (url.port && url.port !== '443') || !/^[a-z0-9][a-z0-9.-]+\.[a-z]{2,}$/i.test(url.hostname) || /(?:^|\.)(?:localhost|local|internal)$/.test(url.hostname)) throw new ScoutError('invalid_cloud_response');
        let response;
        try { response = await fetcher(url.href, {redirect: 'error', signal: AbortSignal.timeout(20000)}); }
        catch { throw new ScoutError('cloud_unavailable'); }
        if (!response.ok) { await response.body?.cancel(); throw new ScoutError('cloud_unavailable'); }
        return {status: 'completed', payload: await boundedJson(response)};
      }
      const payloadSession = await request(`/api/v1/forward/sessions/${run.session_id}`);
      const session = payloadSession?.item ?? payloadSession;
      if (!session || session.id !== run.session_id) throw new ScoutError('invalid_cloud_response');
      if (['terminated', 'failed', 'cancelled'].includes(session.status)) return {status: 'failed'};
      return {status: 'running'};
    },
  };
}
