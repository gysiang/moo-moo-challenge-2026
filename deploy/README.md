# QCA deployment runbook — Moomoo Scout + Guard

**Current policy, 2026-11-20: manual-only.** All four Forward Scout schedules are archived.
Start `python -m dashboard` locally, then use **Run Scout** for research or **Refresh** for
broker holdings and order history. Neither startup nor status polling starts research.
Results are request/session-scoped and automatically imported while the dashboard is open;
after reopening, it resumes result retrieval, not task submission. The published site stays
read-only and is not updated by this local workflow.

**Approve selected** validates all selected hashes, freshness, combined limits, pending
commitments and the existing halt before storing any approvals. Guard runs one serial
batch, rechecking R1-R11 and reconciling each order before proceeding. An uncertain result
blocks further batches; never delete the action/batch journals to retry. No live trading,
Windows startup task, overnight PC, automatic approval or halt clearance is enabled.

The cloud Scout manual protocol is installed. The cloud Guard batch-template update was
blocked by the permission checker; the local client refuses batch dispatch unless that
protocol is confirmed. Trading remains halted. Guard rollout and explicit, reconciled
halt clearance are separate prerequisites. A verified same-ET-day opening equity is also
required; Refresh never invents it. The local changes are not committed or pushed, and
an already-running dashboard must be restarted to load them. Live Forward request and
artifact delivery remain unverified; browser verification uses isolated synthetic data.
Do not assume the cloud repository has the updated Guard bridge. The dry-run coordinator
remains an unused rehearsal. The older notes below do not authorize scheduled runs.

---

## 0. Readiness verdict

| Item | State | Evidence |
|---|---|---|
| Rule gate R1–R11, Guard, adapter, Scout | **Ready** | `python -m unittest discover -s tests -t .` → 284 tests OK |
| Cloud-side order bridge (`guard/bridge.py`) | **Ready** | `tests/test_bridge.py`, 18 tests |
| Deterministic tickets + idempotent retries | **Ready** | `qdr:<ticket>` note, `EventLog(..., append=True)` |
| Exit mandate (R11 exemption, bounded) | **Ready** | `gate/mandate.py`, `guard/mandate_cli.py`, `tests/test_mandate.py` |
| Stale-snapshot Guard refusal | **Ready** | `dashboard/guard.py`, `guard_max_snapshot_age_minutes` (30) |
| QCA account resources | **Not created yet** | no agents, skills, vaults; only Default env `env_00vnzcnwu62gxw85awt9` |
| moomoo remote MCP in cloud | **Unverified** | needs the server URL and an auth method QCA can replay |
| Default environment networking | **Blocked by default** | reports `allow_mcp_servers: false`, `allow_package_managers: false` |
| Skill / vault upload over API | **Not available** | console-only (Skills → Create Skill, Vaults) |
| Human approval inside a cron run | **Impossible by design** | a scheduled run cannot wait for a person |

Consequence: **entry approval stays outside the cloud scheduler.** Scout may run on a cron;
Guard runs on an operator trigger for entries, and on an exit-only cron only while a valid
operator exit mandate is on disk (§4.2). Anything else would quietly break G3/R11, which is
the one guarantee the submission is judged on.

### Cloud-only entry pilot rehearsal (2026-11-20)

`coordinator/` is a standalone, stdlib-only worker for a cloud host with a durable local
volume. It does not depend on the desktop dashboard or its runtime directory. This build
is **DRY_RUN only**: no broker adapter, Guard messages, approvals, live entry mandates,
submission API, enable switch, or resume operation. The existing Guard halt is untouched.
The 24-hour window is a rehearsal clock, not activation of permission to trade.

The immutable rehearsal policy permits both watchlist sleeves at unchanged strategy sizes,
at most two BUYs per ET trading day and 10% of pinned starting equity in cumulative BUY
notional. Reservations remain pending and retain their full quota across restarts and ET
day changes; this build never pretends they filled or frees their quota. Oversized tickets
are rejected, not resized. Fees also reserve cash. R1-R10 are evaluated by the existing
code; R11 remains `R11_NO_APPROVAL`, with hypothetical pilot eligibility reported separately.
Results are `DRY_RUN_RESERVED`, `DRY_RUN_DUPLICATE` or `DRY_RUN_REJECTED`, never `READY`.

Daily starting equity must be explicitly pinned with a dated provenance reference using
`baseline`; a later bundle cannot reset it. Missing baselines, stale evidence, malformed
bundles and unverifiable existing broker BUY usage fail closed. The legacy account schema
has no order notionals, so any same-day executed order or working BUY blocks reservation
until a future broker-reconciliation integration can account for it. This conservative
restriction is intentional; rehearsal success is not execution readiness.

Flow: Scout creates one `coordinator-YYYYMMDD-HHMM.json` using `python -m coordinator.bundle`
(the optional procedure in `scout-agent.md`), then DeliverArtifacts hands it to Cloud Files.
The worker uses authenticated read-only session/file discovery, pinned Scout agent identity,
and same-session bundles. It does not combine separately delivered account/metrics files.
Snapshot age and market-entry window checks happen after download. SQLite WAL, full
synchronous commits and `BEGIN IMMEDIATE` make artifact dedupe and shadow reservations one
transaction. Missing storage stops the worker rather than initializing an empty ledger.

Deployment files: `deploy/coordinator.Dockerfile` and `deploy/coordinator.compose.yaml`.
The Docker-specific ignore file excludes runtime data, `.env`, Guard and broker code from
the build context. Provision an external persistent volume on one cloud host (not a QCA
session filesystem, ephemeral container disk, NFS, or independent per-replica volumes),
owned by UID 10001. Set `COORDINATOR_VOLUME` to its name and `COORDINATOR_ENV_FILE` to the
cloud host's protected `.env` path containing the read-only Cloud API PAT. Nothing provisions
that host or injects credentials automatically. Do not mount the real Guard runtime.

Before starting the service, use its CLI once to initialize a NEW rehearsal database and
pin the verified baseline. `--starts-at` is explicit, expires exactly 24 hours later, and
cannot be renewed by restarting or repeating initialization:

```
python -m coordinator --db /state/coordinator.sqlite3 init-dry-run \
    --pilot-id pilot-rehearsal --starts-at '<UTC_START_ISO>' --scout-agent-id '<SCOUT_AGENT_ID>'
python -m coordinator --db /state/coordinator.sqlite3 baseline \
    --day '<ET_DATE>' --equity '<VERIFIED_OPENING_EQUITY>' \
    --observed-at '<BASELINE_OBSERVATION_ISO>' --source '<PROVENANCE_REFERENCE>'
python -m coordinator --db /state/coordinator.sqlite3 run --env-file /run/secrets/.env
python -m coordinator --db /state/coordinator.sqlite3 status
```

For Compose, run these subcommands via `docker compose -f deploy/coordinator.compose.yaml
run --rm coordinator ...`; the image entrypoint already supplies Python and `--db`.
Then `docker compose -f deploy/coordinator.compose.yaml up -d` starts the read-only worker.
There are no published ports. `revoke` permanently stops that rehearsal; it never contacts
a broker. Poll failures stop rather than silently retry, and restart policy is disabled.
Do not schedule deployment or initialize a real pilot until the host, persistent volume,
Cloud API pagination contract and bundle delivery have been verified in that environment.

Tests: `python -m unittest tests.test_coordinator -v`. No cloud host, persistent volume,
QCA template changes or new schedules are provisioned by these files. Entry execution is a
separate future change requiring current broker reconciliation and explicit operator
permission; this build cannot enable it by configuration.

### Abandoned local Windows rehearsal

The local worker setup was interrupted before any task or pilot database was installed.
The unused launcher sources are retained because their deletion was blocked; they are not
part of the manual dashboard. Do not install them or change Windows execution policy.
The coordinator CLI's optional local log output is tested independently of those launchers.

### Deployed state, 2026-11-17 (created over MCP)

| Resource | ID | State |
|---|---|---|
| Environment `moomoo-paper-env` | `env_00vqvntgwlon5klg6q8z` | created; **networking `limited`** (`allow_mcp_servers: false`), not configurable through the API |
| Agent `moomoo-scout` | `agent_00vqvt6kx064gw66ov25` | version 1, model `auto`, prompt + inlined skill text, `moomoo-skill` bound, **no MCP server** |
| Agent `moomoo-guard` | `agent_00vqvtr9p71fk23t3bku` | version 1, model `auto`, manual-run-only clause, **no MCP server** |
| Skill `moomoo-skill` | `skill_00vqv6tgmo0sgufw77p7` | pre-existing upload; the three challenge skills are **not** uploaded (console only) |
| Deployments | — | **none created on purpose**: with no code mount and no MCP they would run, fail, and consume credits |

What unblocks a real session, in order: (1) attach
`https://github.com/gysiang/moo-moo-challenge-2026` to the agent/session as a repository
resource at `/data/workspace/moo-moo-challenge-2026` (public repo; the MCP tool still demands
a token, so either pass a read-only PAT or clone it from `setup_script` — see §3.3); (2) attach a
remote moomoo MCP URL to the agent (`update_agent`) — the local connector is a
`managed-gateway` marketplace entry with no visible URL, so this has to come from moomoo's
published endpoint or, if the connector is registered platform-side, its QCA connector page;
(3) have the environment networking switched to allow the MCP host. Until all three are
true, the working path is the local one in §8.

---

## 1. Target topology

```
QCA Managed Agent "moomoo-scout"      (cron, America/New_York, read-only moomoo tools)
  quote + klines + news + account    →  runtime/cycle-<slot>.json  (appended to repo/memory)

you, 30 s per ticket                  python -m scout approve  →  runtime/approvals.json

QCA Managed Agent "moomoo-guard"      (manual run, moomoo sim-trade tools)
  guard.bridge_cli prepare            →  runtime/order-plan.json  (READY / REJECTED)
  sim_trade_input_order(arguments)    →  runtime/order-result.json
  guard.bridge_cli record/reconcile   →  runtime/guard-events.jsonl
```

The Scout never holds order tools. The Guard never invents a trade. Both agents run the
**same** `gate/` package, so a prompt injection in Scout cannot widen a limit.

If the cloud MCP path fails at step §3.4, fall back to §8 (local scheduler) — it is
already proven and keeps the competition timeline intact.

---

## 2. Prerequisites

1. Python ≥ 3.11 on the machine you test from (cloud images include it).
2. The repo reachable by QCA: `challenge-agent/` is the root of
   `https://github.com/gysiang/moo-moo-challenge-2026` (public; QCA mounts repositories,
   not folders, and a single uploaded file cannot carry the tree).
3. moomoo MCP server URL: open Qoder Desktop → MCP servers → the `moomoo` entry and copy
   its URL. Do not paste tokens into prompts or files.
4. QCA console access for the two things the API refuses: **Skills** and **Vaults**.

No Python dependencies are installed anywhere: the package is stdlib-only, so the
environment needs no `pip` step and no package-manager permission.

---

## 3. Managed deployment, in order

### 3.1 Upload the three skills (console)

`python deploy/package_skills.py` writes `deploy/dist/*.zip`, one top-level folder per
skill, matching its `name`. Upload each:

Skills → **Create Skill** → upload `moomoo-scout-cycle.zip`, `moomoo-guard-execution.zip`,
`moomoo-paper-reconciliation.zip`. Note each returned skill id; you bind them below.

### 3.2 Create a dedicated environment

```json
{"name": "moomoo-paper-env",
 "description": "Stdlib Python 3 for the moomoo paper challenge; no pip packages",
 "config": {"type": "cloud", "packages": {"pip": []},
            "setup_script": "python3 --version"}}
```

Use it for both agents. Networking is **not** settable on the Environment object. The API
reference lists only `type`, `packages`, `setup_script`, `startup_script`, `storage` for
`config`, and the MCP `update_environment` tool rejects anything but `type`, `packages`,
`setup_script`. The deployed environment reports

```json
{"networking":{"type":"limited","allow_mcp_servers":false,"allow_package_managers":false,"allowed_hosts":[]}}
```

so MCP tool calls out of the sandbox are closed until Qoder enables them for this Environment.
There is no console toggle documented; open a request with Qoder support (`contact@qoder.com`)
or the forum (`https://forum.qoder.com`) naming the environment ID and what it needs.

### 3.3 Mount the code

Attach the repo as a resource. Agent tools start in `/data`, and repositories belong under
`/data/workspace/`:

```json
{"type": "github_repository",
 "url": "https://github.com/gysiang/moo-moo-challenge-2026",
 "mount_path": "/data/workspace/moo-moo-challenge-2026",
 "checkout": {"type": "branch", "name": "main"}}
```

`mount_path` is free-form (it defaults to a path derived from the repo name when omitted).
The repo root *is* the `challenge-agent` tree, so the mount path is the project root — not
`.../moo-moo-challenge-2026/challenge-agent`.

The docs say `authorization_token` is optional for public repos, but the QCA MCP
`create_session` schema marks it **required**. Two ways through:

- supply a fine-grained read-only PAT scoped to this one repo (public, so `Contents: Read`
  is all it needs), pass it inline once, and revoke it after the session; or
- skip the repository resource and clone in `config.setup_script` instead — the image ships
  `git` and the repo is public, so `git clone --depth 1 https://github.com/gysiang/moo-moo-challenge-2026 /data/workspace/moo-moo-challenge-2026`
  needs no credential at all.

Vaults (`vault_ids`) are for secrets the *agent* reads at runtime, not for this mount field.

### 3.4 Prove the MCP path before building agents

Create a throwaway session on the Scout agent (§3.5) and ask it to run
`quote_stock_quote` for `US.SPY` and `quote_market_state`. Three outcomes:

- **works** → continue;
- **401/403** → the URL MCP has no credentials; register the moomoo connector through the
  QCA console connector page (OAuth there), or keep order tools local (§8);
- **timeout / DNS** → environment egress is still closed (`allow_mcp_servers: false`); that
  is a platform-side change for `moomoo-paper-env`, not an API call (see §3.2). Stop and
  raise the request before spending more time.

Do not proceed until this check passes; everything downstream assumes it.

### 3.5 Create the two agents

Agent 1 — `moomoo-scout`:

```json
{"name": "moomoo-scout",
 "description": "Research-only Scout for the moomoo paper challenge (never places orders)",
 "model": "<pick an enabled id from list_models>",
 "system": "<paste deploy/scout-agent.md>",
 "skills": [{"type": "custom", "skill_id": "<moomoo-scout-cycle id>", "version": "latest"},
            {"type": "custom", "skill_id": "<moomoo-paper-reconciliation id>", "version": "latest"}],
 "mcp_servers": [{"name": "moomoo", "type": "url", "url": "<moomoo MCP URL>"}],
 "tools": [{"type": "agent_toolset_20260401",
            "enabled_tools": ["Bash", "Read", "Write", "Glob", "Grep"]},
           {"type": "mcp_toolset", "mcp_server_name": "moomoo",
            "configs": [
              {"name": "sim_trade_input_order", "enabled": false},
              {"name": "sim_trade_cancel_order", "enabled": false},
              {"name": "sim_trade_modify_order", "enabled": false}]}]}
```

Disabling the order tools on Scout is a second lock: the prompt says "never", the config
makes it impossible.

Agent 2 — `moomoo-guard`:

```json
{"name": "moomoo-guard",
 "description": "Sole execution path for approved paper orders",
 "model": "<pick an enabled id from list_models>",
 "system": "<paste deploy/guard-agent.md>",
 "skills": [{"type": "custom", "skill_id": "<moomoo-guard-execution id>", "version": "latest"},
            {"type": "custom", "skill_id": "<moomoo-paper-reconciliation id>", "version": "latest"}],
 "mcp_servers": [{"name": "moomoo", "type": "url", "url": "<moomoo MCP URL>"}],
 "tools": [{"type": "agent_toolset_20260401", "enabled_tools": ["Bash", "Read", "Write"]},
           {"type": "mcp_toolset", "mcp_server_name": "moomoo",
            "permission_policy": {"type": "always_ask"}}]}
```

### 3.6 Create four Scout deployments (one per slot)

A five-field cron cannot express four irregular times, so create four deployments sharing
the same agent/environment; each carries its slot in the initial message.

| Slot (ET) | cron | id suffix |
|---|---|---|
| 09:45 | `45 9 * * 1-5` | 0945 |
| 11:00 | `0 11 * * 1-5` | 1100 |
| 13:30 | `30 13 * * 1-5` | 1330 |
| 15:30 | `30 15 * * 1-5` | 1530 |

```json
{"name": "scout-0945",
 "agent": "<moomoo-scout id>",
 "environment_id": "<moomoo-paper-env id>",
 "resources": [ <repo resource from §3.3> ],
 "environment_variables": "QDR_ACCOUNT=5829804;QDR_MARKET=100",
 "schedule": {"type": "cron", "expression": "45 9 * * 1-5", "timezone": "America/New_York"},
 "initial_events": [{"type": "user.message",
   "content": [{"type": "text",
     "text": "Run the 09:45 ET Scout cycle exactly as moomoo-scout-cycle describes. Market closed or holiday => stop after one line. Finish with the ticket table."}]}]}
```

`America/New_York` in the schedule handles DST; `scout/slots.py` does the same in code, so
they agree. Holidays still fire the cron — the first check inside the skill is
`quote_trading_days`, and the run exits before any quote call.

### 3.7 Verify, then keep it read-only for a day

`run_deployment` once per slot, then read `get_deployment_run` + `list_session_events`.
Acceptance for day 1: four `cycle-<slot>.json` files, each with a `skipped` list and no
REJECTED record caused by a bug (R10 staleness is a legitimate result). Compare one run
against a local `scout plan` on the same raw file: identical ticket ids and gate codes.

---

## 4. Daily operating loop (both modes)

1. **Slot fires** → Scout writes `runtime/cycle-<slot>.json`. Tickets are
   `P-YYYYMMDD-HHMM-SYMBOL-NN`.
2. **You review** `AWAITING_APPROVAL` records only, using the printed rationale, cost,
   % of equity and gate codes. HOLDs show as `REJECTED` with `R10_EVIDENCE_STALE`.
3. **Approve** (single ticket, 12 h TTL from `approval_ttl_minutes`, hash-bound):
   ```
   python -m scout approve --cycle runtime/cycle-0945.json --ticket P-... \
       --approvals runtime/approvals.json
   ```
   The dashboard's **Approve** button does this and then triggers the Guard run for you —
   see §4.1. The 12-hour window is what makes the 22:45 / 00:00 SGT cycles usable: an
   evening approval is still valid later in the same US session.
4. **Guard run** (operator trigger for entries; exit-only cron only under a mandate, §4.2):
   ```
   python -m guard.bridge_cli prepare --cycle runtime/cycle-0945.json --ticket P-... \
       --account runtime/account-5829804.json --approvals runtime/approvals.json \
       --metrics runtime/metrics.json --out runtime/order-plan.json
   ```
   Exit 0 = READY, exit 2 = REJECTED with the rule code. If it is not READY, stop.
5. **Submit** `order-plan.json.arguments` verbatim with `sim_trade_input_order`; save the
   raw reply to `runtime/order-result.json`.
6. **Record and reconcile**:
   ```
   python -m guard.bridge_cli record --order runtime/order-plan.json \
       --result runtime/order-result.json --log runtime/guard-events.jsonl
   python -m guard.bridge_cli reconcile --log runtime/guard-events.jsonl \
       --order-id <id> --submitted <n> --filled <n> --status <status> --position-qty <n>
   ```
7. **Kill switch** (any surprise): cancel every working order, then
   `python -m guard.bridge_cli kill --log runtime/guard-events.jsonl --reason "..." --cancelled <ids>`.
   A later `resume` is only legal after a reconciliation line exists.

Never edit `order-plan.json`. A changed size or limit invalidates the approval hash and
the gate will reject it at step 4 with `R11_NO_APPROVAL`. That is the contract working.

### 4.1 Operator dashboard (local approvals + Guard trigger)

```
python -m dashboard --runtime runtime/demo --port 8765   # paper artefacts the pipeline wrote
python -m dashboard --demo --port 8765                   # synthetic fixtures, never mutated
python -m dashboard --runtime runtime/demo --no-cloud --port 8765   # skip Cloud reads
```

Serves `http://127.0.0.1:8765` (loopback only; non-loopback Host/Origin is refused). It
reads `account-5829804.json`, `metrics.json`, `cycle-*.json`, `approvals.json`,
`rejections.jsonl`, `guard-triggers.jsonl`, `guard-events.jsonl` and `equity-history.jsonl`
from `--runtime` (default `runtime/`), and appends the current equity point on each load
when it has moved. If the directory has no `account-5829804.json`, the KPI row says so
instead of rendering a fake −100%. Demo mode reads and writes only the `*-demo` files, so
the fixtures are never mutated. Poll interval is 30 s.

**Approve → Guard.** **Approve** on an `AWAITING_APPROVAL` row writes the same hash-bound
record as step 3 (cycle `proposal_hash`, 12 h TTL), flips the row to `APPROVED`, then
posts a Guard trigger to the persistent Forward Guard session
(`QODER_GUARD_SESSION_ID` in `.env`, or `--guard-session`; `--no-guard` disables it). The
trigger message carries a `guard-trigger` envelope: the cycle record, the account
snapshot, `metrics.json` and the approval record, all inline — the cloud sandbox has no
copy of `runtime/`. The Guard re-runs `guard.bridge_cli prepare` on that evidence and only
submits if the gate is READY; steps 6–7 still run by hand. A click is the per-ticket human
authorization, so `sim_trade_input_order` is `always_allow` while cancel stays
`always_ask` and every modify/unlock path stays `always_deny` (see
[`guard-agent.md`](guard-agent.md)). Each attempt is appended to
`runtime/guard-triggers.jsonl` with a `sending` line before the POST, then `accepted`, or
`rejected`/`unknown`. Only an HTTP refusal may be retried ("Run Guard" reappears); an
`unknown` outcome stays blocked until the Guard session is checked by hand. Demo mode never
triggers the Guard.

**Snapshot freshness.** A trigger is refused when the cycle `generated_at`, the account
`as_of`, or `metrics.as_of` is older than `guard_max_snapshot_age_minutes` (30 by default,
`config.json`) or more than 5 minutes in the future. The approval is still recorded — only
the Guard run is blocked — and the row shows the age. Run a fresh Scout cycle first; a stale
snapshot is how a wrong quantity or price reaches the broker.

**Mandated exits.** With an active exit mandate (§4.2) a `READY` full-position SELL row shows
**Execute exit** instead of Approve. The click writes no approval: the server re-checks the
mandate hash, bounds and per-run cap, then sends the same envelope with
`"authorization": "MANDATE"`, `"mandate_id"`, and `evidence.approvals` `{}`. A partial trim,
a BUY, an expired mandate or a gate rejection returns 409 and nothing is sent. The mandate
panel shows id, account, expiry and max exits per run — never the operator's note.

**Reject → log.** **Reject** appends `{ticket_id, cycle_file, proposal_hash, symbol, side,
quantity, limit_price, est_cost, gate_codes, rejected_at, actor, reason}` to
`runtime/rejections.jsonl`, flips the row to `REJECTED_BY_OPERATOR` and blocks a later
approval of the same ticket (`reject_ticket` also refuses when a live approval already
exists, so the order of approve/reject is never ambiguous). Repeat clicks are idempotent.

**Cloud agents card.** The bottom card lists recent Cloud Agents sessions (Scout cron runs
included) and expands to each session's public event log. Reads go through the dashboard
server: it loads `QODER_PAT_TKN` from `.env` (`--env-file`, default `.env`; gitignored) at
startup and calls `https://api.qoder.com/api/v1/cloud` with
`Authorization: Bearer <PAT>` from `GET /api/cloud/sessions` and `/api/cloud/events` only.
The browser talks to loopback alone — the PAT is never serialized into a response, never in
`app.js`, and error messages are redacted. Without the key (or with `--no-cloud`) the card
shows "disabled" and the rest of the dashboard works unchanged.

---

### 4.2 Exit mandate and the exit-only schedule (Path B, 2026-11-19)

Overnight protection cannot depend on a human being awake, and the paper broker has no
server-side stop order (`order_type` is limit or market only). The operator therefore signs
a **bounded exit mandate**; Guard may execute the exits it covers on a schedule. Entries are
unaffected: they still need a per-ticket hash-bound approval.

Grant / inspect / revoke — operator commands only, never an agent's:

```
python -m guard.mandate_cli grant --account-id 5829804 --hours 72 \
    --granted-by "<operator>" --note "overnight stop protection" \
    --reasons all --max-exits 6
python -m guard.mandate_cli show          # exit 1 when absent, expired or tampered
python -m guard.mandate_cli revoke --reason "competition window closed"
```

`--hours` must be in (0, 168]; replacing an active mandate needs `--replace`. `--reasons`
defaults to all four (`EXIT_CORE_BELOW_SMA50`, `EXIT_BELOW_SMA50`, `EXIT_STOP_LOSS`,
`EXIT_TREND_CROSS`) or a comma-separated subset. The file is sha256-self-bound: editing any
field makes `load_mandate` raise, and every consumer (gate, Scout, bridge CLI, dashboard)
then refuses to act. `revoke` archives it as `exit-mandate.revoked-<ts>.json` and appends
`MANDATE_REVOKED` to the event log.

What the mandate covers, all enforced in `gate/mandate.py` before R11 passes:

- account `5829804` only, and only inside the grant→expiry window (300 s clock skew);
- `SELL` of the **entire** held quantity, with an `exit_reason` in the allowed list;
- at most `max_exits_per_run` exits per run, counted from `ORDER_SUBMITTED` events in
  `runtime/guard-events.jsonl` for the run key `<cycle date>:<slot>` — not from a number an
  agent supplies;
- R1–R10 still apply in full. A BUY, a partial trim, a new position, or an unheld symbol is
  `R11_NO_APPROVAL`. An unconfirmed broker outcome still halts everything, exits included.

Exit-only cycle (Scout, between the four entry slots):

```
python -m scout plan --exit-only --raw runtime/market-raw.json \
    --account runtime/account-5829804.json --approvals runtime/approvals.json \
    --out runtime/cycle-<HHMM>.json
```

No `--slot`: the label is the current ET time, entries are blocked, only held positions are
examined, and news is not required for a SELL. Without a mandate the exits come back
`AWAITING_APPROVAL`; with one they come back `READY`.

Schedule (Forward **Schedule**, not a Managed Deployment — the agents are Forward templates).
The Guard refuses any snapshot older than 30 minutes, so the Scout cycle must run first and
the Guard a few minutes later. Proposed cadence, **not created yet — needs operator
confirmation**:

| Run | cron (America/New_York) | Message |
|---|---|---|
| Scout exit-only | `*/30 10-15 * * 1-5` | "Run an exit-only Scout cycle per moomoo-scout-cycle. No entries." |
| Guard exit-only | `5,35 10-15 * * 1-5` | "Scheduled exit-only run: verify the mandate, then execute the READY SELL records of the newest cycle." |

The 15:30 entry-slot cycle is already exits-only, so the last Guard pass of the day covers it.
On a holiday or a closed session both runs stop after one line. If the mandate is absent,
expired or tampered, the Guard run submits nothing and reports that exits need per-ticket
approval — that is the intended failure mode, not something to work around.

---

## 5. What the cloud cannot do

- **Wait for entry approval.** Hence entries are operator-triggered; the only Guard cron is
  the exit-only run of §4.2, and only while a valid mandate is on disk.
- **Upload skills or vaults.** Console only; budget five minutes.
- **Reach moomoo without an authorized MCP registration.** §3.4 is the gate.
- **Keep state between runs.** Every run is a fresh session; persist `runtime/` in the
  mounted repo (commit each run) or a memory store. The event log is append-only JSONL,
  so commits only grow it.

## 6. Failure policy in the cloud

| Symptom | Action |
|---|---|
| `prepare` exits 2 | log the code, do not submit, continue to the next ticket |
| Submit returns no `order_id` | `record` writes `ORDER_UNKNOWN` + `HALTED`; stop all runs, reconcile, `resume` with a note |
| Broker rejects explicitly | `record` writes `ORDER_REJECTED`; no halt; safe to retry with a **new** ticket |
| Duplicate ticket replay | deterministic ticket id → second `prepare` is indistinguishable; the `qdr:` note and `ORDER_EXISTING` make the paper broker dedupe visible |
| MCP timeout during Scout | skip the slot, log `RUN_SKIPPED`; never invent a price |
| Snapshot older than 30 min | dashboard refuses the trigger; Guard reports "stale snapshot"; run a fresh cycle, never refresh data inside Guard |
| Mandate absent / expired / hash mismatch | exit-only run submits nothing and reports that exits need per-ticket approval |
| Exit beyond `max_exits_per_run` | `prepare` returns `R11_NO_APPROVAL`; report as skipped, do not raise the cap |

## 7. Teardown

`pause_deployment` (not archive) between sessions so a run can be resumed on 20 Nov;
`archive_deployment` + `archive_agent` after the window. Leave the environment and vault
until the submission is graded, then `archive_environment`.

## 8. Local fallback (already proven)

If §3.4 fails, keep execution local and still submit cloud evidence. During the window
EST is UTC−5 and SGT is UTC+8, so ET + 13h:

| Slot (ET) | Local time (SGT) | schtasks `/st` |
|---|---|---|
| 09:45 Mon | 22:45 Mon | 22:45 |
| 11:00 Mon | 00:00 Tue | 00:00 |
| 13:30 Mon | 02:30 Tue | 02:30 |
| 15:30 Mon | 04:30 Tue | 04:30 |

ET Monday sessions land on Tuesday local time, so the task days are **TUE–SAT**:

```
schtasks /create /tn "qdr-scout-0945" /tr "python -m scout plan --slot 09:45 --raw runtime/market-raw.json --account runtime/account-5829804.json --news runtime/news.json --out runtime/cycle-0945.json" /sc weekly /d TUE,WED,THU,FRI,SAT /st 22:45
```

(Repeat for the other three slots with `--slot 11:00|13:30|15:30` and the times above.)
`python -m scout due` printed by the wrapper confirms which slot is live before planning.
The market feed still has to be written beforehand (`python -m scout.ingest` from a quote
dump), which is why the cloud Scout is preferable when it works.

Approval and Guard stay as §4 steps 3–7, run by hand. The same skills run in Qoder
Desktop locally — the SKILL.md files reference relative paths, so nothing needs
rewriting for a local run.
