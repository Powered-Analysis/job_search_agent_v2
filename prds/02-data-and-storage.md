# Data & Storage
#### tl;dr

One hosted libSQL (Turso) database is the single source of truth for job-posting *data* — search output, the canonical-URL idempotency key, the full job description, and the user's fit feedback — shared by the headless cloud cron and every local session. This spec owns the schema, the idempotent insert, the connection/transaction contract, the query surface every other step reads and writes through, the append-only search-telemetry table, the cron's daily claim table, the learning-loop run-tracking table, and schema creation and change. It deliberately stores **no application state** (that lives in the Google Sheet tracker, PRD 04).

------
#### Goals

##### Business Goals
- **One copy, no divergence (`XC-2`):** cloud cron and local sessions connect to the *same* database, so posting data can never fork between two stores.
- **Safe re-runs:** a single-mechanism idempotency guard (`UNIQUE(canonical_url)` + `INSERT … ON CONFLICT DO NOTHING RETURNING id`) makes every re-encounter a no-op and lets callers distinguish genuinely-new rows (`XC-3`).
- **Durable writes:** every statement autocommits, so a single review decision or pipeline insert persists immediately — closing a silent-data-loss trap in the Turso client (`XC-8`).
- **Schema evolves without data loss:** additive columns and CHECK-constraint changes apply automatically on open via an ordered, self-healing rebuild that never leaves the data absent from a table named `postings`.
- **Telemetry outlives the rows it describes:** search findings and learning-loop run records survive deletion/reset of the `postings` rows they reference.

##### User Goals
- As the job seeker, my fit feedback and every captured JD are stored reliably and are never silently dropped.
- As the job seeker, the same posting never appears twice no matter how often searches overlap.

##### Non-Goals
- **Application state** (Date Applied, Status) — never stored here; owned by the Google Sheet tracker's user columns (PRD 04). Authority never flows Sheet→DB (`XC-4`).
- **URL canonicalization and filesystem-safe naming logic** — produced by PRD 01 (`canonicalize.py`, `naming.py`); this spec only stores the results and enforces the `UNIQUE` constraint.
- **What the columns *mean* downstream** (review, packet naming, tracker rows, refinement scope) — owned by PRDs 03/04/05; this spec owns the columns and the queries, and references those consumers.
- **A second dedup subsystem or embeddings** (`XC-3`).

-----
#### User Stories

**Job seeker**
- As the job seeker, I want my Apply/Skip decision and note to be committed the instant I make it, so that an interrupted review session loses nothing.

**Developer**
- As the developer, I want the DB layer to be transport-agnostic (hosted Turso over HTTP, or a local `file:` SQLite for throwaway dev), so that I can run against a local replica by changing one env var.
- As the developer, I want a schema change to apply automatically on the next open (no separate migrate step to forget), and to be self-healing if a connection drops mid-migration.
- As the developer, I want the live schema and the migration target held in one place, so they cannot drift apart.

**Operator**
- As the operator, I want a row to be individually deletable over the HTTP transport (e.g. to roll back a learning-loop run), which requires every table to have a stable primary key.

-----
#### Functional Requirements

**Connection & transaction contract (Priority: P0)**
- **Transport-agnostic (`db.connect`):** a `file:` URL opens a local SQLite file via stdlib `sqlite3` (throwaway dev only); any other URL connects to hosted Turso via `turso_serverless` (DB-API 2.0 over Hrana/HTTP — the managed platform serves HTTP, not WebSockets, so `libsql-client`/native libsql are not used). The rest of the module treats both identically.
- **Autocommit via `isolation_level = None` (P0, `XC-8`):** `turso_serverless` mirrors stdlib sqlite3's *legacy* transaction model; only `isolation_level = None` actually enables autocommit. The DB-API `autocommit` attribute it also exposes is stored but never consulted — a silent no-op — so without this every DML opens an implicit `BEGIN DEFERRED` that `close()` rolls back, silently dropping the write. Both the pipeline insert and the review loop depend on this.

**The `postings` table (Priority: P0)**
- **Single table** holding: `id`; `company`, `title`, `url`, `date_posted`; `canonical_url` (`NOT NULL UNIQUE` — the idempotency key); `normalized_company`, `title_slug`; `jd_markdown`, `location`; `search_agent` (`CHECK IN ('claude','perplexity','gemini','manual')`); `first_seen_at`; `decision` (`CHECK IN ('Apply','Skip')`, nullable until reviewed); `fit_feedback`; `decided_at`; `added_to_tracker` (default 0).
  - *Stored vs displayed (`XC` note):* `decision` stores the exact strings `Apply`/`Skip` — these double as display labels; downstream logic keys off the stored value. `search_agent` stores the wire value `claude`/`perplexity`/`gemini`/`manual`.
- **No application state:** the table deliberately omits Date Applied / Status; those are the Sheet's user columns (`XC-4`).
- **Single-source column list:** the columns live once in `_POSTINGS_COLUMNS` (and an ordered `_POSTINGS_COLUMN_NAMES`), which both the `CREATE` and the migration rebuild consume, so the live schema and the migration target cannot drift.

**Idempotent insert & JD capture (Priority: P0)**
- **`insert_posting`** uses `INSERT … ON CONFLICT(canonical_url) DO NOTHING RETURNING id`: returns the new id, or `None` if the row already existed — the signal the pipeline uses to fetch a JD only for genuinely-new rows. A `manual` add sets `decision = 'Apply'` *in the INSERT itself* (with `decided_at` set in the same statement via a `CASE`), so the row is never briefly visible as undecided; searched rows leave `decision`/`decided_at` NULL.
- **`update_jd_capture`** stores `jd_markdown` + `location`; when a non-empty ATS-canonical `title` is provided it also overwrites `title` and re-derives `title_slug` (via `naming.slugify_title`) so packet naming carries the canonical title. A `None`/empty title leaves title and slug untouched — the rule that keeps a failed refetch from blanking a good capture (PRD 04).

**Query surface (Priority: P0)** — the seams other steps read/write through:
- `pending_review` — `decision IS NULL`, oldest first (Step 3 backlog, PRD 03).
- `record_decision` / `set_decision` / `clear_decision` — write Apply/Skip (+ feedback); refresh `decided_at` on every write (so amendments/promotions re-enter refinement scope); `clear_decision` nulls all three and, deliberately, does **not** touch telemetry (see below).
- `find_by_canonical_url` — a UX-only read for the manual-add path (not the idempotency guard).
- `pending_tracker` (`Apply AND added_to_tracker = 0`) — the Step 5 idempotency guard (PRD 04).
- `pending_packets` (`Apply`; default also `added_to_tracker = 0`, waived by `posting_id`) — the Step 4 queue (PRD 04).
- `rows_for_refetch` (default `Apply`; `--all`/`--id` widen) — the reconciliation scope (PRD 04).
- `mark_tracked` — set `added_to_tracker = 1` (Step 5 tail).
- `rows_for_refinement` / `decided_history` / `refinement_cutoff` — the learning-loop scope (PRD 05).
- `claim_cron_run` — the cron's once-per-day claim (below; PRD 01).

**Search-telemetry table `search_findings` (Priority: P1)**
- **Append-only per-agent coverage telemetry** (coverage / cross-agent overlap / Apply-precision): one row per `(run_date, agent, canonical_url)` — its composite primary key — for *every* posting an agent returns, whether or not the idempotent `postings` insert no-ops, so both agents get credit for a shared find (the overlap a `postings`-only view hides). `agent CHECK IN ('claude','perplexity','gemini')`; carries `window_hours`, `rank`, `found_at`, a denormalized `decision`, and:
  - `verification` (`CHECK IN ('verified','verified_no_date','not_on_index','out_of_window','unverifiable','unsupported')`) — the pipeline's liveness and recency outcome (PRD 01). `NOT NULL`. Every emitted posting is recorded, not only inserted ones, so each agent's dead-link and out-of-window rates are computable.
  - `ats_date`, `ats_date_kind` (`CHECK IN ('updated','created','published')`) — the ATS timestamp the recency check used and what it measures; NULL when the platform returned none or verification stopped before reaching it.
  - `model`, `effort` — what actually ran (the Perplexity model as reported by the API; `effort` NULL where the runner has none), so coverage, dead-link rate, and Apply-precision can be compared per model/effort choice, not just per runner (`XC-14`).
- **Written before the insert** (`record_finding`, `ON CONFLICT DO NOTHING`) so a no-op'd req still credits the finding agent (PRD 01).
- **Denormalized `decision` (P0 invariant):** kept current by `sync_finding_decision` (matched on `canonical_url`, not a posting id — there is deliberately no FK) from `record_decision`/`set_decision`, **never** from `clear_decision` (un-deciding is not a new decision; the last real call stays as the historical record). The column is retained as raw evaluation telemetry — per-agent coverage, overlap, and Apply-precision can be computed straight off `search_findings`, and survive a `postings` row being deleted or reset — but the pipeline deliberately ships **no built-in report** over it (the repo's `analysis/` R script is one external consumer). **Never** delete or bulk-rewrite `search_findings` outside this sync path.

**Cron claim table `cron_runs` (Priority: P0)**
- One row per day the scheduled search ran: `run_date` (the date in the profile's `timezone`, `PRIMARY KEY`) and `claimed_at` (default now). `claim_cron_run(run_date)` is `INSERT … ON CONFLICT(run_date) DO NOTHING RETURNING run_date` and returns whether this call won the day — the same single-mechanism idempotency as the `postings` insert (`XC-3`), so however many hourly wakes reach the gate, exactly one runs the day's searches (PRD 01).
- Written before the searches run and never updated, so a row means "attempted", not "succeeded"; the run summary in `fly logs` is where outcome lives. Deleting a day's row by hand lets the next wake rerun it.

**Learning-loop run table (Priority: P1)**
- **`prompt_refinement_runs`** — one row per run, with an AUTOINCREMENT `id`, `run_at` (default now), `considered`, `changed`. `MAX(run_at)` is the next run's scope cutoff, which is what keeps the loop incremental without a watermark in the artifact itself (PRD 05). A run is recorded only on success; an errored run records nothing so its inputs are reconsidered.
  - *Rationale for the `id`:* libSQL over Hrana/HTTP refuses a DELETE against a table with no stable row identity, so the surrogate key is what lets a run be rolled back by hand.

**Schema creation and change (Priority: P0)**
- **`init_db` creates the schema** — `postings`, `search_findings`, `cron_runs`, `prompt_refinement_runs` — with `CREATE TABLE IF NOT EXISTS` from the single-source column lists. It is idempotent and every command calls it, so there is no separate setup step to forget. The schema holds only tables the application reads or writes.
- **How a future schema change ships:** as a migration `init_db` runs on open, *before* the creates (a rebuild briefly parks data under another table name, and a create run first would fill that window with an empty table, orphaning the real rows). An additive column uses plain `ALTER TABLE ADD COLUMN`; a CHECK change uses the rebuild (create → copy by name → rename → rename → drop), because SQLite cannot ALTER a CHECK constraint.
- **Ordered and self-healing, because hosted Turso is not atomic across statements** (each is its own round-trip): every migration is ordered so no step leaves the data absent from its named table, ships with a recovery routine that finishes a half-done swap on the next open, and is a no-op once applied (one `sqlite_master` read).

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa init-db` creates the `postings` table and its satellites (idempotent — every command calls `init_db`, so there is no separate migrate step). Requires `TURSO_DATABASE_URL`.
- Local dev: set `TURSO_DATABASE_URL=file:dev.db` for a throwaway SQLite file.

**Core Experience (developer)**
- All access flows through `db.py`; no other module opens a connection or writes SQL. Callers pass a `Connection` (either backend) and use the typed query functions above.

**Edge Cases**
- **Interrupted migration:** self-healed on the next `init_db` by that migration's recovery routine; the data is never absent from its named table for a committed step.
- **`postings` row deleted or reset to undecided:** `search_findings` retains its denormalized decision, so telemetry still counts correctly; `clear_decision` leaves the last decision on findings intact.
- **Missing `TURSO_DATABASE_URL`:** `load_config` raises a clear error (PRD 06).

-----
#### Technical Considerations
- **Client choice (`turso_serverless`):** chosen because Turso's managed platform serves Hrana over HTTP only; the native/WebSocket clients don't fit. DB-API 2.0 keeps the module transport-agnostic against local `sqlite3`.
- **Autocommit trap (`XC-8`):** the single most load-bearing connection detail — see the transaction contract above.
- **No ORM:** hand-written SQL, single-file (`db.py`) ownership; the column list is centralized to prevent schema/migration drift.
- **HTTP transport constraints:** no cross-statement atomicity guarantee (drives the ordered, self-healing schema-change procedure) and a hard requirement for stable primary keys to allow DELETE (drives the surrogate `id`s).

-----
#### Integration Points
- **Turso (hosted libSQL)** via `turso_serverless` (DB-API 2.0 / Hrana over HTTP). Auth: `TURSO_DATABASE_URL` (+ `TURSO_AUTH_TOKEN`). Same database from cloud and local (`XC-2`).
- **stdlib `sqlite3`** for `file:` dev URLs.
- Consumers: PRD 01 (insert, JD capture, findings, cron claim), PRD 03 (review reads/writes), PRD 04 (tracker/packet queues, refetch, `mark_tracked`), PRD 05 (refinement scope + run recording).

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06):
- A **Turso** account and database; `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` set locally (`.env`) and as Fly secrets.
- Run `jsa init-db` once against the target database (also implicitly ensured by every command).

-----
#### Outstanding Questions
- *(none surfaced during verification — code and docs align on this subsystem.)*
