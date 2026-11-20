import { ScoutError } from './scout-report.mjs';

export function scoutStore(db) {
  const rows = result => {
    if (result.error || !Array.isArray(result.data)) throw new ScoutError('scout_storage_unavailable');
    return result.data;
  };
  return {
    async read(owner) {
      const found = rows(await db.from('scout_state').select('*').eq('owner_id', owner).limit(1));
      return found[0] ?? null;
    },
    async insert(row) {
      const result = rows(await db.from('scout_state').insertIfAbsent(row, {onConflict: 'owner_id'}).select('*'));
      return result[0] ?? null;
    },
    async change(owner, version, values) {
      const result = rows(await db.from('scout_state').update({...values, version: version + 1}).eq('owner_id', owner).eq('version', version).select('*'));
      return result[0] ?? null;
    },
  };
}
