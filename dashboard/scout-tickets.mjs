import {execFile} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {ScoutError} from '../functions/scout-report.mjs';

const project = fileURLToPath(new URL('../', import.meta.url));

export function ticketEvaluator(python = 'python') {
  return async (payload, run, asOf) => {
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => /^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP|LOCALAPPDATA)$/i.test(key)));
    try {
      const source = JSON.stringify({payload, run: {request_id: run.request_id, started_at: run.started_at}, as_of: new Date(asOf).toISOString()});
      if (Buffer.byteLength(source) > 2000000) throw new Error();
      const output = await new Promise((accept, reject) => {
        const child = execFile(python, ['-B', '-E', '-s', '-m', 'dashboard.scout_tickets'],
          {cwd: project, env, timeout: 15000, maxBuffer: 2000000, windowsHide: true},
          (error, stdout) => error ? reject(error) : accept(stdout));
        child.stdin.on('error', reject);
        child.stdin.end(source);
      });
      const cycle = JSON.parse(output);
      if (cycle.request_id !== run.request_id || cycle.trading_enabled !== false || cycle.review_only !== true ||
          !Array.isArray(cycle.records) || !Array.isArray(cycle.skipped)) throw new Error();
      return cycle;
    } catch { throw new ScoutError('ticket_generation_unavailable'); }
  };
}
