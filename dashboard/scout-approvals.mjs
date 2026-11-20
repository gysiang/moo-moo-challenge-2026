import config from '../config.json' with {type: 'json'};
import {ScoutError} from '../functions/scout-report.mjs';

const hash = value => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
const checks = Array.from({length: 11}, (_, i) => `R${i + 1}`);

export function scoutApprovals({store, now = Date.now}) {
  const ttl = config.approval_ttl_minutes * 60000;
  if (!Number.isSafeInteger(ttl) || ttl <= 0) throw new Error('Invalid approval lifetime');
  const blocked = (run, record, time) => {
    const report = run.last_report, cycle = report?.cycle;
    if (run.status !== 'completed' || report?.request_id !== run.request_id || cycle?.request_id !== run.request_id || report.ticket_checks !== 'completed') return 'report_not_current';
    if (cycle.review_only !== true || cycle.trading_enabled !== false || !hash(record.proposal_hash)) return 'ticket_not_eligible';
    if (record.status !== 'AWAITING_APPROVAL' || cycle.baseline_verified !== true || record.gate?.passed !== false ||
        record.gate.codes?.length !== 1 || record.gate.codes[0] !== 'R11_NO_APPROVAL' ||
        !checks.every(code => record.gate.checked?.includes(code))) return 'ticket_not_eligible';
    if (cycle.historical) return 'historical_assessment';
    const generated = Date.parse(cycle.generated_at);
    if (!Number.isFinite(generated) || generated > time + 5000 || time >= generated + ttl) return 'ticket_review_expired';
    return null;
  };
  const inspect = async (run, time) => {
    if (!run?.last_report?.cycle) return [];
    const saved = await store.approvals(run.owner_id, run.last_report.request_id);
    return run.last_report.cycle.records.map(record => {
      const approval = saved.find(item => item.ticket_id === record.ticket_id && item.proposal_hash === record.proposal_hash) ?? null;
      const status = approval ? (time >= Date.parse(approval.expires_at) ? 'EXPIRED' : 'APPROVED') : null;
      const reason = blocked(run, record, time) || (approval ? status === 'EXPIRED' ? 'approval_expired' : 'already_approved' : null);
      return {ticket_id: record.ticket_id, proposal_hash: record.proposal_hash, eligible: !reason, reason, status, approval};
    });
  };
  return {
    async view(run) { return {enabled: true, ttl_minutes: config.approval_ttl_minutes, tickets: await inspect(run, now())}; },
    async approve(run, body) {
      if (!Number.isSafeInteger(body.version) || body.version < 1 || !Array.isArray(body.tickets) || !body.tickets.length || body.tickets.length > 21 ||
          body.tickets.some(item => !item || typeof item.ticket_id !== 'string' || !/^[A-Za-z0-9._-]{1,100}$/.test(item.ticket_id) || !hash(item.proposal_hash) || Object.keys(item).some(key => !['ticket_id', 'proposal_hash'].includes(key))) ||
          new Set(body.tickets.map(item => item.ticket_id)).size !== body.tickets.length) throw new ScoutError('invalid_input', 400);
      if (run.status !== 'completed' || run.last_report?.request_id !== run.request_id) throw new ScoutError('report_not_current', 409);
      const time = now(), tickets = await inspect(run, time), pending = [];
      for (const chosen of body.tickets) {
        const ticket = tickets.find(item => item.ticket_id === chosen.ticket_id);
        if (!ticket || ticket.proposal_hash !== chosen.proposal_hash) throw new ScoutError('ticket_changed', 409);
        if (!ticket.eligible && ticket.reason !== 'already_approved') throw new ScoutError(ticket.reason, 409);
        if (!ticket.approval) pending.push({ticket_id: ticket.ticket_id, proposal_hash: ticket.proposal_hash,
          approved_at: new Date(time).toISOString(), expires_at: new Date(time + ttl).toISOString()});
      }
      if (!pending.length) return run;
      if (body.version !== run.version) throw new ScoutError('version_conflict', 409);
      const saved = await store.saveApprovals(run.owner_id, run.version, run.request_id, pending);
      if (!saved) throw new ScoutError('version_conflict', 409);
      return saved;
    },
  };
}
