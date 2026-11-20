import { ScoutError, scoutReport } from './scout-report.mjs';
import { boundedJson } from './scout-cloud.mjs';

const ACTIVE = new Set(['creating', 'sending', 'running', 'unknown']);
const CODES = new Set(['login_required', 'invalid_user_context', 'invalid_input', 'not_found', 'method_not_allowed', 'scout_storage_unavailable', 'scout_not_configured', 'run_active', 'version_conflict', 'cooldown', 'dispatch_unknown', 'dispatch_rejected', 'cloud_unavailable', 'invalid_cloud_response', 'response_too_large', 'invalid_scout_result', 'scout_failed', 'scout_timeout', 'ticket_generation_unavailable']);
const requestId = value => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
const response = (payload, status = 200) => Response.json(payload, {status, headers: {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}});

export function scoutApplication({store, cloud, requireUser, now = Date.now, evaluateTickets}) {
  const fail = (code, status) => { throw new ScoutError(code, status); };
  const check = row => {
    if (row && (!Number.isSafeInteger(row.version) || row.version < 1 || row.version >= 2147483646 || !requestId(row.request_id) || ![...ACTIVE, 'completed', 'failed'].includes(row.status) || !Number.isFinite(Date.parse(row.started_at)))) fail('scout_storage_unavailable', 503);
    return row;
  };
  const read = async owner => check(await store.read(owner));
  const view = row => {
    const expired = row && ACTIVE.has(row.status) && now() - Date.parse(row.started_at) > 1800000;
    return {ok: true, configured: cloud.configured(), version: row?.version ?? 0, request_id: row?.request_id ?? null,
      status: expired ? 'unknown' : row?.status ?? 'idle', started_at: row?.started_at ?? null, completed_at: row?.completed_at ?? null,
      next_run_at: row ? new Date(Date.parse(row.started_at) + 300000).toISOString() : null,
      error_code: expired ? 'scout_timeout' : CODES.has(row?.error_code) ? row.error_code : null,
      report: row?.last_report ?? null, trading_enabled: false};
  };
  const change = async (owner, run, values) => {
    const changed = await store.change(owner, run.version, values);
    if (!changed) fail('version_conflict', 409);
    return check(changed);
  };
  return async ctx => {
    try {
      const request = ctx.request, url = new URL(request.url);
      if (!/^\/(?:functions\/v1\/)?(?:app|app-d[0-9a-f]{32})\/?$/.test(url.pathname)) fail('not_found', 404);
      const action = url.searchParams.get('action');
      if (!['scout-status', 'scout-start', 'scout-sync'].includes(action)) fail('not_found', 404);
      if (request.method !== (action === 'scout-status' ? 'GET' : 'POST')) fail('method_not_allowed', 405);
      const user = requireUser(ctx);
      const owner = user.id;
      if (typeof owner !== 'string' || !owner) fail('login_required', 401);
      if (action === 'scout-status') return response(view(await read(owner)));
      if (!request.headers.get('content-type')?.startsWith('application/json')) fail('invalid_input', 400);
      let body;
      try { body = await boundedJson(request, 1024); } catch { fail('invalid_input', 400); }
      if (!body || Array.isArray(body) || !requestId(body.request_id)) fail('invalid_input', 400);
      const keys = action === 'scout-start' ? ['request_id', 'version'] : ['request_id'];
      if (Object.keys(body).some(key => !keys.includes(key))) fail('invalid_input', 400);
      let run = await read(owner);
      if (action === 'scout-start') {
        if (!Number.isSafeInteger(body.version) || body.version < 0) fail('invalid_input', 400);
        if (run?.request_id === body.request_id) return response(view(run));
        if (run && ACTIVE.has(run.status)) fail('run_active', 409);
        if ((run?.version ?? 0) !== body.version) fail('version_conflict', 409);
        if (run && now() - Date.parse(run.started_at) < 300000) fail('cooldown', 429);
        if (!cloud.configured()) fail('scout_not_configured', 503);
        const intent = {request_id: body.request_id, started_at: new Date(now()).toISOString(), status: 'creating', session_id: null, event_ids: [], completed_at: null, error_code: null};
        if (run) run = await change(owner, run, intent);
        else {
          run = check(await store.insert({...intent, owner_id: owner, version: 1, last_report: null}));
          if (!run) fail('run_active', 409);
        }
        // An unreconciled intent never expires into permission to create or send again.
        try {
          const session_id = await cloud.create(run);
          run = await change(owner, run, {session_id, status: 'sending'});
          const event_ids = await cloud.send(run);
          run = await change(owner, run, {status: 'running', event_ids});
        } catch (error) {
          const definitive = error instanceof ScoutError && error.code === 'dispatch_rejected';
          try {
            run = await store.change(owner, run.version, {status: definitive ? 'failed' : 'unknown', error_code: definitive ? 'dispatch_rejected' : 'dispatch_unknown'}) || await read(owner);
          } catch { fail('scout_storage_unavailable', 503); }
        }
        return response(view(run), 202);
      }
      if (!run || run.request_id !== body.request_id) fail('not_found', 404);
      const backfill = Boolean(evaluateTickets && run.status === 'completed' && run.last_report?.request_id === run.request_id && run.last_report.ticket_checks === 'not_run');
      if ((!ACTIVE.has(run.status) && !backfill) || !run.session_id) return response(view(run));
      try {
        const collected = await cloud.collect(run);
        let values;
        if (collected.status === 'completed') {
          const evaluatedAt = now(), evidenceTime = backfill ? Date.parse(run.completed_at) : evaluatedAt;
          if (!Number.isFinite(evidenceTime)) fail('invalid_scout_result', 422);
          const report = scoutReport(collected.payload, run, evidenceTime);
          if (backfill && JSON.stringify(report) !== JSON.stringify(run.last_report)) fail('invalid_scout_result', 422);
          if (evaluateTickets) {
            report.cycle = await evaluateTickets(collected.payload, run, evidenceTime);
            report.cycle.evaluated_at = new Date(evaluatedAt).toISOString();
            report.cycle.historical = backfill;
            report.ticket_checks = 'completed';
          }
          values = {status: 'completed', completed_at: backfill ? run.completed_at : new Date(evaluatedAt).toISOString(), error_code: null, last_report: report};
        } else if (backfill) fail('cloud_unavailable', 503);
        else if (collected.status === 'failed') values = {status: 'failed', error_code: 'scout_failed'};
        else if (now() - Date.parse(run.started_at) > 1800000) values = {status: 'unknown', error_code: 'scout_timeout'};
        if (values) run = await store.change(owner, run.version, values) || await read(owner);
      } catch (error) {
        if (!backfill && error instanceof ScoutError && error.code === 'invalid_scout_result') {
          run = await store.change(owner, run.version, {status: 'failed', error_code: 'invalid_scout_result'}) || await read(owner);
        } else throw error;
      }
      return response(view(run));
    } catch (error) {
      const code = CODES.has(error?.code) ? error.code : 'scout_storage_unavailable';
      const status = code === 'login_required' ? 401 : code === 'invalid_user_context' ? 403 : error instanceof ScoutError ? error.status : 503;
      return response({ok: false, error: code}, status);
    }
  };
}
