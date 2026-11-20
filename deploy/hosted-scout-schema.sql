CREATE TABLE app.scout_state (
  owner_id text PRIMARY KEY,
  version integer NOT NULL,
  request_id text NOT NULL,
  started_at timestamptz NOT NULL,
  status text NOT NULL,
  session_id text,
  event_ids jsonb NOT NULL,
  completed_at timestamptz,
  error_code text,
  last_report jsonb
);
