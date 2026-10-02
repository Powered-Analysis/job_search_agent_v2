# Platform & Operations
#### tl;dr

How the system runs: the **cloud/local split** (only Steps 1–2 run headless in the cloud; everything else, including deployment, runs locally), the **Fly.io** deployment that runs the scheduled search cron and the **`jsa deploy`** command that ships it, the **`jsa` CLI** surface, the **configuration surface and `profile/` layout**, and — most importantly for anyone standing this up — the **complete inventory of user-interactive setup and secrets**. This spec is the single home for the cross-cutting operational facts every other PRD references, and for the one-time human setup the whole product depends on.

------
#### Goals

##### Business Goals
- **Unattended search in the cloud, at a fixed time:** a single Fly `hourly` machine runs the search cron with no human in the loop, self-gating to the weekly schedule and the profile's `run_at`, so the run time never drifts and never needs re-establishing.
- **Credentials stay where they belong:** the Claude, Perplexity, and Gemini API keys live as Fly secrets (cloud) and in local `.env`; Google OAuth and Fly auth stay on the local machine; no credential ever passes through an agent transcript (`XC-1`).
- **Only the search profile leaves the machine:** the image carries app code plus `profile/search/`, nothing else of the user's (`XC-11`).
- **One reproducible setup path:** a documented, ordered sequence takes a fresh clone to a running cron and a working local pipeline.
- **One command to ship:** `jsa deploy` validates the search profile, builds the image, and swaps it onto the scheduled machine in place.
- **Cheap to run:** shared-CPU, 1 GB Fly machine; the cloud image carries only what Steps 1–2 need.

##### User Goals
- As the operator, I want to configure one schedule and have the right searches run on the right days, at the time I chose, automatically.
- As the operator, I want a clear, ordered checklist of every account, key, and file I must set up, so that nothing is discovered in production.

##### Non-Goals
- **The behavior of each command** — owned by PRDs 01–05; this spec owns *where each runs* and *what it needs to run*.
- **The DB connection contract** — PRD 02 (referenced here as an operational fact).
- **Prompt templates and their slot tables** — PRDs 01, 04, 05 (`XC-13`); this spec owns where the profile files live, not what each slot means.

-----
#### User Stories

**Operator**
- As the operator, I want the cloud image to run exactly one entrypoint (`jsa cron`), so that the deployment surface is minimal and predictable.
- As the operator, I want one local command (`jsa deploy`) to ship my current code and search profile by swapping the machine's image in place, so that the schedule is preserved and no CI or git push is involved.
- As the operator, I want the memory sized so the Claude Code CLI subprocess doesn't hang, so that cloud runs are reliable.

**Developer**
- As the developer, I want to run the whole pipeline locally against a throwaway SQLite file, so that I can develop without touching the hosted DB.
- As the developer, I want every external tool path overridable by an env var, so that tests and scratch runs can stub them.

-----
#### Functional Requirements

**Cloud/local split (`XC-1`) (Priority: P0)**
- **Cloud (Fly.io):** Steps 1–2 (`jsa cron` → `jsa search`) only. Headless, automated, no local disk or Google OAuth.
- **Local:** Steps 3–5 (`review`, `add`, `packet`, `generate`, `track`, `refetch`), the learning loop (`refine`), and `deploy` — they need a terminal, Chrome, the local disk (`profile/`, packet directories), the `gws` OAuth token, and `flyctl`. There is no CI.
- **One shared Turso DB** for both sides (`XC-2`); `jsa init-db` (run once locally) creates it, and every command ensures the schema on connect.

**Deployment image (`Dockerfile`) (Priority: P0)**
- Base `python:3.14-slim-bookworm`. Installs Node 20 + the `@anthropic-ai/claude-code` CLI (the Claude Agent SDK spawns it), `uv`, `tzdata`. Runs as non-root `appuser` (uid 1001, `HOME=/app`) because the Claude Code CLI refuses to run as root without explicit flags. Entrypoint: `uv run --no-dev jsa cron`.
- **Copies the app plus `profile/search/` and nothing else of the profile.** `.dockerignore` excludes `profile/` except `profile/search/`, so the rest of the profile is never even in the build context — which matters because Fly's remote builder uploads the context off the machine (`XC-11`).
- No `TZ` anchor: the cadence and window use the profile's `timezone` explicitly (PRD 01).

**Fly configuration (`fly.toml`) (Priority: P0)**
- **Carries no user identity:** no `app` name or `primary_region` — `jsa deploy` passes both from `[fly]` in `profile/config.toml` (`XC-11`). **`memory = 1024 MB` is mandatory** — the Claude Code CLI subprocess hangs on the 256 MB default. No `[deploy]` release block: machines run the image directly (no `fly deploy` release lifecycle); secrets are set with `--stage`. The schedule is applied at machine-create time, not in the toml.

**Deployment sequence (user-run; sets billed secrets — never via an agent) (Priority: P0)**
1. **Turso:** `turso db create` → `turso db show --url` (→ `TURSO_DATABASE_URL`) → `turso db tokens create` (→ `TURSO_AUTH_TOKEN`); put both in local `.env`; `uv run jsa init-db`.
2. **Auth:** `claude setup-token` (→ `CLAUDE_CODE_OAUTH_TOKEN`) *or* an `ANTHROPIC_API_KEY` — **never both** (the CLI prefers the API key and 401s the OAuth flow); a `PERPLEXITY_API_KEY`; a `GEMINI_API_KEY` if the schedule uses `gemini`.
3. **Fly:** `fly auth login` → `fly apps create <app>` → set `[fly] app` and `region` in `profile/config.toml` → `fly secrets set --stage -a <app> TURSO_DATABASE_URL=… TURSO_AUTH_TOKEN=… CLAUDE_CODE_OAUTH_TOKEN=… PERPLEXITY_API_KEY=… [GEMINI_API_KEY=…]`.
4. **Smoke test:** `jsa deploy --smoke` (runs one ungated `jsa cron` on a throwaway machine, exits).
5. **Schedule:** `jsa deploy` (creates the `hourly` machine on first run; it self-gates to `search.toml`).
6. **Updates:** `jsa deploy` again, after any code change or an accepted refine proposal (PRD 05).

**`jsa deploy` (`deploy.py`) (Priority: P0)** — local only; uses the user's `flyctl` session (no deploy token).
1. **Validate before building:** assemble the search prompt and parse `search.toml` exactly as the cloud will (`XC-13`); any missing required fragment, invalid schedule, or malformed runner setting aborts before a build. Model and agent IDs are not checked against a list (`XC-14`) — `--smoke` is where a rejected ID surfaces. Warns (does not abort) on a schedule that leaves a gap in the week, on a `run_at` after 22:59 (a slipped wake can cross midnight and miss the day, PRD 01), and on a pending refine proposal (it will not ship).
2. **Build and push:** `fly deploy --build-only --push --image-label <UTC stamp> -a <app>`.
3. **Swap in place:** find the machine carrying the `hourly` schedule. None → create it (`fly machine run <image> --schedule hourly --vm-memory 1024 --region <region>`). One → `fly machine update <id> --image <image> --vm-memory 1024 --schedule hourly`, retrying for registry lag; re-asserting the schedule on every update means an image swap can never drop it. Whatever the update does to Fly's interval anchor is harmless, because the run time comes from the gate, not the anchor (PRD 01). More than one → error (ambiguous; the user resolves it in Fly).
- **`--dry-run`:** step 1 plus the list of profile files that would ship; no build.
- **`--smoke`:** steps 1–2, then one `jsa cron --ungated` on a `--rm` machine: it skips the time-of-day gate and the daily claim (so it never consumes the day's scheduled run) and runs today's scheduled searches, if any; the scheduled machine is untouched.
- **Never sets secrets** — those stay a user-run step (above).

**CLI surface (`cli.py`) (Priority: P0)** — entry point `jsa = jsa.cli:main`:

| Command | Step | Where | Cloud? |
|---|---|---|---|
| `init-db` | — | local (once) + cloud | via image |
| `search --agent --window-hours` | 1–2 | either | called by cron |
| `cron` | 1–2 | Fly | **yes (only entrypoint)** |
| `add <URL>` | 3 (skips) | local | no |
| `review` | 3 | local | no |
| `refetch [--id/--all/--dry-run]` | recon | local | no |
| `packet [--id/--dry-run]` | 4 head | local | no |
| `generate [--id/--dry-run]` | 4 | local | no |
| `track [--id/--dry-run]` | 5 | local | no |
| `refine [--dry-run/--accept/--reject]` | learn | local | no |
| `deploy [--dry-run/--smoke]` | ops | local | ships the image |

**Configuration surface (`config.py` + env + `profile/`) (Priority: P0)** — three kinds of input, three homes (`XC-11`).

*Environment — secrets and machine-local settings only* (every one listed, commented, in `.env.example`):
- **Required everywhere:** `TURSO_DATABASE_URL` (raises if unset). `TURSO_AUTH_TOKEN` required for hosted Turso (omit for a `file:` dev URL).
- **Command-specific:** `PERPLEXITY_API_KEY` / `GEMINI_API_KEY` (their runners' searches; validated lazily so other commands run without them); Claude auth `CLAUDE_CODE_OAUTH_TOKEN` *or* `ANTHROPIC_API_KEY` (read by the SDK's CLI from the inherited env — not by `config.py`).
- **Optional overrides (default):** `JSA_PROFILE_DIR` (`./profile`), `JSA_GWS_BIN` (`gws`), `JSA_GENERATE_WORKERS` (3).

*The profile — everything about the user* (gitignored in full; `profile.example/` is committed with the identical shape and a fictional candidate):

```
profile/
  config.toml             local   candidate_name (optional), tracker_spreadsheet_id,
                                  packets_dir (default ~/Documents/Job Applications),
                                  [fly] app + region,
                                  [agents.checklist|refine] model + effort   (XC-14)
  resume.docx             local   the single base resume                    (PRD 04)
  search/                 SHIPS IN THE FLY IMAGE
    search.toml                 timezone; run_at; [schedule] weekday → ordered (agent, window_hours);
                                [runners.claude] model + effort; [runners.gemini] agent  (PRD 01)
    candidate.md, target_roles.md, filters.md,
    positive_signals.md, negative_signals.md, hard_exclusions.md               (PRD 01 slots)
  refine/                 local   a pending refine proposal: rationale + conflict-marked fragments (PRD 05)
```

- **Validated at load:** each TOML file is parsed into a typed config; an unknown key raises (a typo never silently falls back to a default), and a key a command needs but the profile lacks raises naming the file and pointing to `profile.example/`. Requiredness is per command: `tracker_spreadsheet_id` for `track`/`generate`/`refetch`; `resume.docx` for `packet`/`generate`/`refetch`; `[fly]` for `deploy`; each `[agents.*]` for its command; each scheduled runner's `[runners.*]` for `search`/`cron`/`deploy`. Model and effort values are checked for form only, never against a list of allowed models (`XC-14`); `profile.example/` carries the recommended defaults as comments.
- **The profile is only data.** No profile file is executable or imported as code; the app reads it through `config.py` and `prompts.assemble` (`XC-13`).

**Complete user-setup inventory (Priority: P0)** — the consolidated home; other PRDs reference this:

*Accounts & cloud (one-time):* Turso account + DB; Fly.io account + `fly apps create` + the scheduled machine (created by `jsa deploy`); Perplexity account + key; a Google AI Studio key (if using `gemini`); Anthropic auth (`claude setup-token` for the OAuth token, or an API key).

*Secrets — where each lives:*
- Local `.env`: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `PERPLEXITY_API_KEY`, `GEMINI_API_KEY` (if used), one of `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`.
- Fly secrets (`--stage`): `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `PERPLEXITY_API_KEY`, `GEMINI_API_KEY` (if used), one Claude credential.

*Local tools:* Google Chrome (review); the `gws` CLI + `gws auth login` (Sheets — note the testing-status OAuth 7-day token expiry until the consent screen is published); `flyctl` + `fly auth login` (deploy); `uv`; the Claude Code CLI (for local Claude-driven commands).

*The profile the user seeds (`XC-11`)* — `cp -r profile.example profile`, then replace the fictional candidate: `config.toml`; the six search fragments and `search.toml`; `resume.docx` (the single base resume). No app prompt needs an edit.

*Outside the profile:* the Google Sheet (Applications tab, A:H header, Status dropdown / data-validation).

*One-time commands:* `uv run jsa init-db`; `jsa deploy`.

-----
#### User Experience

**Entry Point & First-Time Experience**
- A fresh clone: `uv sync` → set `.env` → `cp -r profile.example profile` and make it yours → `jsa init-db` → (cloud) the Fly sequence above, ending in `jsa deploy`. `README.md` ("Using this for your own search") carries the full ordered walkthrough.
- Local-only dev: `TURSO_DATABASE_URL=file:dev.db` for a throwaway SQLite file (`XC-2`).

**Core Experience**
- *Cloud:* the machine wakes hourly; `jsa cron` exits within seconds unless it is a scheduled day, at or after `run_at`, and the day is unclaimed — in which case it claims the day, runs the searches, writes to Turso, and stops.
- *Local:* the human works the pipeline — `review` → `generate` → (auto) `track`, then the user's own revision of each packet's resume copy against its checklist, with `refetch`/`refine` as periodic maintenance; an accepted refine proposal or a code change reaches the cloud only via `jsa deploy`.

**Edge Cases**
- **256 MB machine:** the CLI subprocess hangs — always pass `--vm-memory 1024`.
- **Both Claude credentials set:** the API key wins and OAuth 401s — provide exactly one.
- **`gws` token expired (exit code 2):** re-run `gws auth login` (until the consent screen is published).
- **Missing `TURSO_DATABASE_URL`:** every command raises a clear pointer to `.env.example`.
- **Missing or invalid profile file / key:** the command that needs it raises, naming the file and pointing to `profile.example/`.
- **Schedule dropped after an image update:** cannot persist — `jsa deploy` re-asserts `--schedule hourly` on every update.
- **More than one `hourly` machine:** `jsa deploy` refuses rather than guess which to update.
- **A deploy or manual start wakes the machine mid-day:** the gate decides as on any wake; it cannot cause a second run or move the next one.

-----
#### Technical Considerations
- **No `fly deploy` release lifecycle:** machines run images directly; updates are in-place image swaps that preserve the schedule.
- **Fly's schedule carries no time of day:** intervals are counted from the machine's creation, and whether an update or manual start resets that count is undocumented. Hourly wakes plus the in-app gate make the run time independent of it. The cost is about 24 short wakes a day, each billed per second on the 1 GB machine and exiting before any DB or model call on all but one of them.
- **One timezone, from the profile:** the cadence gate, the search window, and the tracker's Date Added all use `timezone` from `profile/search/search.toml`, never the image's `TZ` or the host's zone, so cloud and local agree on what "today" is.
- **Claude auth is never passed by code (`XC-1`):** the SDK's spawned CLI reads it from the inherited environment; this is why it is not on `Config`.
- **`turso_serverless` over HTTP (`XC-2`, PRD 02)** is what makes the same DB reachable identically from Fly and locally.

-----
#### Integration Points
- **Fly.io** (`flyctl`, the user's local `fly auth login` session) — hosting + scheduler; driven by `jsa deploy`.
- **Turso** (PRD 02).
- **Perplexity / Gemini / Anthropic** APIs (PRD 01).
- **Google Workspace** via the local `gws` CLI (PRD 04).
- **Chrome, `uv`, Node/Claude Code CLI** — local/image tooling.

-----
#### Outstanding Questions
- **Google OAuth consent screen is unpublished.** Until it is published in GCP, the `gws` refresh token expires every ~7 days (exit code 2 → re-run `gws auth login`). This is a known, tracked one-time setup gap, not a code defect.
- **Claude auth rotation.** The intended end state is the subscription OAuth token in both homes (`.env`, Fly secrets); wherever an `ANTHROPIC_API_KEY` still coexists it must be unset to avoid the 401 trap.
