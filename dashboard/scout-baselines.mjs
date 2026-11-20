import {ScoutError} from '../functions/scout-report.mjs';
import {openingRecordValidator} from './scout-tickets.mjs';

const dayFormat = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'});
const dayAt = time => dayFormat.format(new Date(time));
const summary = baseline => baseline ? {
  trading_day: baseline.record.trading_day, as_of: baseline.record.as_of,
  opening_equity: baseline.record.opening_equity, source_reference: baseline.record.source_reference,
  sha256: baseline.sha256, imported_at: baseline.imported_at, verification: baseline.verification,
} : null;

export function scoutBaselines({store, now = Date.now, python = 'python'}) {
  const validate = openingRecordValidator(python);
  return {
    async forTime(owner, time) { return store.baseline(owner, dayAt(time)); },
    async view(owner, run) {
      const current_day = dayAt(now()), current = await store.baseline(owner, current_day);
      const report_day = run?.last_report ? dayAt(run.last_report.as_of) : null;
      const report = report_day ? await store.baseline(owner, report_day) : null;
      return {enabled: true, current_day, current: summary(current), report: summary(report),
        reassessment_available: Boolean(report && run.status === 'completed' && run.last_report.request_id === run.request_id &&
          run.last_report.cycle?.baseline_verified === false)};
    },
    async import(owner, body) {
      if (!body || typeof body !== 'object' || Array.isArray(body) || body.confirmed !== true ||
          Object.keys(body).some(key => !['source_text', 'confirmed'].includes(key)) ||
          typeof body.source_text !== 'string' || Buffer.byteLength(body.source_text) > 16384)
        throw new ScoutError('invalid_opening_record', 400);
      const baseline = await validate(body.source_text, now());
      const saved = await store.saveBaseline(owner, baseline.record.trading_day, baseline);
      if (saved.sha256 !== baseline.sha256) throw new ScoutError('opening_record_conflict', 409);
    },
  };
}
