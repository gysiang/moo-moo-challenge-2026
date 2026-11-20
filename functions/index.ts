import { createDatabaseClient, createSiteContext, requireUser } from './_qoder/backend.mjs';
import { scoutStore } from './scout-store.mjs';
import { scoutCloud } from './scout-cloud.mjs';
import { scoutApplication } from './scout-routes.mjs';

const handle = scoutApplication({
  store: scoutStore(createDatabaseClient()),
  cloud: scoutCloud({getToken: () => Deno.env.get('QODER_PAT')}),
  requireUser,
});
Deno.serve(async request => {
  try { return await handle(createSiteContext(request)); }
  catch {
    return Response.json({ok: false, error: 'scout_storage_unavailable'}, {status: 503, headers: {'Cache-Control': 'no-store'}});
  }
});
