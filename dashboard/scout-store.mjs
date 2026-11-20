import {DatabaseSync} from 'node:sqlite';
import {closeSync, openSync} from 'node:fs';
import {ScoutError} from '../functions/scout-report.mjs';

const APPLICATION_ID = 0x53434f54;
const FIELDS = ['request_id', 'started_at', 'status', 'session_id', 'event_ids', 'completed_at', 'error_code', 'last_report'];
const jsonFields = new Set(['event_ids', 'last_report']);
const encode = (key, value) => jsonFields.has(key) ? JSON.stringify(value) : value;
const decode = row => row ? {...row, event_ids: JSON.parse(row.event_ids), last_report: JSON.parse(row.last_report)} : null;

export function sqliteScoutStore(path) {
  let db;
  try {
    try { closeSync(openSync(path, 'wx', 0o600)); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    db = new DatabaseSync(path, {timeout: 5000});
    db.exec('BEGIN IMMEDIATE');
    try {
      const version = db.prepare('PRAGMA user_version').get().user_version;
      const id = db.prepare('PRAGMA application_id').get().application_id;
      const tables = db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").get().n;
      if (version === 0 && id === 0 && tables === 0) {
        db.exec(`CREATE TABLE scout_state (
          owner_id TEXT PRIMARY KEY NOT NULL,
          version INTEGER NOT NULL CHECK (version > 0),
          request_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('creating','sending','running','unknown','completed','failed')),
          session_id TEXT,
          event_ids TEXT NOT NULL CHECK (json_valid(event_ids)),
          completed_at TEXT,
          error_code TEXT,
          last_report TEXT NOT NULL CHECK (json_valid(last_report))
        ); PRAGMA application_id = ${APPLICATION_ID}; PRAGMA user_version = 1;`);
      } else if (![1, 2, 3].includes(version) || id !== APPLICATION_ID) {
        throw new Error('Not a Scout database');
      }
      if (version < 2) db.exec(`CREATE TABLE scout_approvals (
        owner_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        proposal_hash TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        PRIMARY KEY (owner_id, request_id, ticket_id, proposal_hash)
      ) STRICT; PRAGMA user_version = 2;`);
      if (version < 3) db.exec(`CREATE TABLE scout_baselines (
        owner_id TEXT NOT NULL,
        trading_day TEXT NOT NULL,
        evidence TEXT NOT NULL CHECK (json_valid(evidence)),
        PRIMARY KEY (owner_id, trading_day)
      ) STRICT;
      CREATE TRIGGER scout_baselines_no_update BEFORE UPDATE ON scout_baselines
        BEGIN SELECT RAISE(ABORT, 'Opening records are immutable'); END;
      CREATE TRIGGER scout_baselines_no_delete BEFORE DELETE ON scout_baselines
        BEGIN SELECT RAISE(ABORT, 'Opening records are immutable'); END;
      PRAGMA user_version = 3;`);
      db.exec('COMMIT');
    } catch (error) { db.exec('ROLLBACK'); throw error; }
    db.exec('PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL');
    const read = db.prepare('SELECT * FROM scout_state WHERE owner_id = ?');
    const insert = db.prepare(`INSERT INTO scout_state (owner_id, version, ${FIELDS.join(', ')})
      VALUES (${Array(10).fill('?').join(', ')}) ON CONFLICT(owner_id) DO NOTHING RETURNING *`);
    const safe = action => {
      try { return decode(action()); } catch { throw new ScoutError('scout_storage_unavailable'); }
    };
    return {
      async read(owner) { return safe(() => read.get(owner)); },
      async insert(row) { return safe(() => insert.get(row.owner_id, row.version, ...FIELDS.map(key => encode(key, row[key])))); },
      async change(owner, version, values) {
        return safe(() => {
          const keys = Object.keys(values);
          if (!keys.length || keys.some(key => !FIELDS.includes(key))) throw new Error('Invalid state fields');
          const update = db.prepare(`UPDATE scout_state SET ${keys.map(key => `${key} = ?`).join(', ')}, version = version + 1 WHERE owner_id = ? AND version = ? RETURNING *`);
          return update.get(...keys.map(key => encode(key, values[key])), owner, version);
        });
      },
      async approvals(owner, request) {
        try {
          return db.prepare('SELECT ticket_id, proposal_hash, approved_at, expires_at FROM scout_approvals WHERE owner_id = ? AND request_id = ? ORDER BY ticket_id').all(owner, request);
        } catch { throw new ScoutError('scout_storage_unavailable'); }
      },
      async saveApprovals(owner, version, request, approvals) {
        return safe(() => {
          db.exec('BEGIN IMMEDIATE');
          try {
            const row = db.prepare("UPDATE scout_state SET version = version + 1 WHERE owner_id = ? AND version = ? AND request_id = ? AND status = 'completed' RETURNING *").get(owner, version, request);
            if (!row) { db.exec('ROLLBACK'); return null; }
            const insertApproval = db.prepare('INSERT INTO scout_approvals (owner_id, request_id, ticket_id, proposal_hash, approved_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)');
            for (const item of approvals) insertApproval.run(owner, request, item.ticket_id, item.proposal_hash, item.approved_at, item.expires_at);
            db.exec('COMMIT');
            return row;
          } catch (error) { db.exec('ROLLBACK'); throw error; }
        });
      },
      async baseline(owner, day) {
        try {
          const row = db.prepare('SELECT evidence FROM scout_baselines WHERE owner_id = ? AND trading_day = ?').get(owner, day);
          return row ? JSON.parse(row.evidence) : null;
        } catch { throw new ScoutError('scout_storage_unavailable'); }
      },
      async saveBaseline(owner, day, evidence) {
        try {
          db.prepare('INSERT INTO scout_baselines (owner_id, trading_day, evidence) VALUES (?, ?, ?) ON CONFLICT(owner_id, trading_day) DO NOTHING').run(owner, day, JSON.stringify(evidence));
          return JSON.parse(db.prepare('SELECT evidence FROM scout_baselines WHERE owner_id = ? AND trading_day = ?').get(owner, day).evidence);
        } catch { throw new ScoutError('scout_storage_unavailable'); }
      },
      close() { db.close(); },
    };
  } catch {
    db?.close();
    throw new ScoutError('scout_storage_unavailable');
  }
}
