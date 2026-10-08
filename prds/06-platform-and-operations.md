# Platform & Operations
#### tl;dr

How the system runs: the **cloud/local split** (the search and the email side door run headless in the cloud; everything else, including deployment, runs locally), the **Fly.io** deployment of the scheduled search machine and the inbox machine and the **`jsa deploy`** command that ships both, the **`jsa` CLI** surface, the **configuration surface and `profile/` layout**, and — most importantly for anyone standing this up — the **complete inventory of user-interactive setup and secrets**. This spec is the single home for the cross-cutting operational facts every other PRD references, and for the one-time human setup the whole product depends on.

------
#### Goals

##### Business Goals
- **Unattended search in the cloud, at a fixed time:** a single Fly `hourly` machine runs the search cron with no human in the loop, self-gating to the weekly schedule and the profile's `run_at`, so the run time never drifts and never needs re-establishing.
- **Apply from anywhere:** a second Fly app's `hourly` machine drains the jobs mailbox, so an emailed posting becomes a packet in Drive and a tracker row while the user's computer is off (PRD 03, 04).
- **Credentials stay where they belong:** the Claude, Perplexity, and Gemini API keys live as Fly secrets (cloud) and in local `.env`; Fly auth stays on the local machine; Google credentials leave it only for the inbox app, each scoped to its one job; no credential ever passes through an agent transcript (`XC-1`).
- **Only what each machine needs leaves the computer:** the image carries app code plus `profile/search/`, nothing else of the user's; the inbox machine also gets `config.toml` and `resume.docx`, as machine files, never in the image (`XC-11`).
- **One reproducible setup path:** a documented, ordered sequence takes a fresh clone to a running cron and a working local pipeline.
- **One command to ship:** `jsa deploy` validates the profile, builds the image, and swaps it onto both scheduled machines in place.
- **Cheap to run:** shared-CPU, 1 GB Fly machines that wake hourly and exit within seconds when there is nothing to do; no machine runs between wakes.

##### User Goals
- As the operator, I want to configure one schedule and have the right searches run on the right days, at the time I chose, automatically.
- As the operator, I want a clear, ordered checklist of every account, key, and file I must set up, so that nothing is discovered in production.
- As the operator, I want to email a job from my phone and have it built and tracked without my computer, so that the pipeline doesn't wait for me to be at my desk.

##### Non-Goals
- **The behavior of each command** — owned by PRDs 01–05; this spec owns *where each runs* and *what it needs to run*.
- **The DB connection contract** — PRD 02 (referenced here as an operational fact).
- **Prompt templates and their slot tables** — PRDs 01, 04, 05 (`XC-13`); this spec owns where the profile files live, not what each slot means.

-----
#### User Stories

**Operator**
- As the operator, I want the cloud image to run exactly two entrypoints, `jsa cron` on the search machine and `jsa inbox` on the inbox machine, so that the deployment surface is minimal and predictable.
- As the operator, I want neither cloud machine to accept an inbound connection, so that the cloud has no endpoint to attack.
- As the operator, I want one local command (`jsa deploy`) to ship my current code and search profile by swapping the machine's image in place, so that the schedule is preserved and no CI or git push is involved.
- As the operator, I want the memory sized so the Claude Code CLI subprocess doesn't hang, so that cloud runs are reliable.

**Developer**
- As the developer, I want to run the whole pipeline locally against a throwaway SQLite file, so that I can develop without touching the hosted DB.
- As the developer, I want every external CLI the app shells out to (`gws`, `fly`, `pandoc`) overridable by an env var, so that tests and scratch runs can stub them.

-----
#### Functional Requirements

**Cloud/local split (`XC-1`) (Priority: P0)**
- **Cloud (Fly.io), two apps, headless and scheduled, neither accepting inbound connections:**
  - **The search app:** Steps 1–2 (`jsa cron` → `jsa search`). No local disk and no Google credential.
  - **The inbox app** (only when `[inbox]` is configured): the email side door (`jsa inbox`, PRD 03) and the one-posting build and track it continues into (PRD 04).
  - Separate apps keep separate secrets, so the search machine never holds a Google credential.
- **Local:** Steps 3–5 (`review`, `add`, `packet`, `generate`, `track`, `refetch`), the learning loop (`refine`), and `deploy` — they need a terminal, Chrome, the local disk (`profile/`, packet directories), the `gws` login, and `flyctl`. The inbox machine also runs `add`, and `generate` for one posting at a time, for the email side door. No deployment depends on CI or a git push (`XC-1`).
- **One shared Turso DB** for both sides (`XC-2`); `jsa init-db` (run once locally) creates it, and every command ensures the schema on connect.

**Deployment image (Priority: P0)**
- Python 3.14, timezone data, and the Claude Code CLI the Claude Agent SDK drives (the SDK bundles it). It runs as a non-root user with a writable home directory, because the Claude Code CLI refuses to run without permission prompts as root. Its entrypoint is `jsa cron`; the inbox machine overrides it with `jsa inbox`. One image serves both machines, so it also carries the `gws` CLI, `pandoc`, and `typst` the inbox's packet build needs. No dev dependencies. Base image and install mechanics are the engineers' call, on current, supported releases.
- **Copies the app plus `profile/search/` and nothing else of the profile.** The build context excludes `profile/` except `profile/search/`, so the rest of the profile is never even in the build context — which matters because Fly's remote builder uploads the context off the machine (`XC-11`).
- No `TZ` anchor: the cadence and window use the profile's `timezone` explicitly (PRD 01).

**Fly configuration (`fly.toml`) (Priority: P0)**
- **Carries no user identity:** no `app` name or `primary_region` — `jsa deploy` passes both from `profile/config.toml` (`[fly]`, and `[inbox] app` for the inbox app) (`XC-11`). One `fly.toml` serves both apps. **`memory = 1024 MB` is mandatory** — the Claude Code CLI subprocess hangs on the 256 MB default. No `[deploy]` release block: machines run the image directly (no `fly deploy` release lifecycle); secrets are set with `--stage`. The schedule is applied at machine-create time, not in the toml.

**Deployment sequence (user-run; sets billed secrets — never via an agent) (Priority: P0)**
1. **Turso:** `turso db create` → `turso db show --url` (→ `TURSO_DATABASE_URL`) → `turso db tokens create` (→ `TURSO_AUTH_TOKEN`); put both in local `.env`; `uv run jsa init-db`.
2. **Auth:** a `JSA_SEARCH_ANTHROPIC_API_KEY` (an Anthropic Console API key; production Claude search runs use it, PRD 01) if the schedule uses `claude`; a `PERPLEXITY_API_KEY` if it uses `perplexity`; a `GEMINI_API_KEY` if it uses `gemini`. For the local Claude commands (checklist, refine), whichever Claude credential the user prefers in local `.env`: `claude setup-token` (→ `CLAUDE_CODE_OAUTH_TOKEN`) *or* an `ANTHROPIC_API_KEY` — **never both** (the CLI prefers the API key and 401s the OAuth flow).
3. **Fly:** `fly auth login` → `fly apps create <app>` → set `[fly] app` and `region` in `profile/config.toml` → stage the cloud's secrets from `.env` with `fly secrets import --stage -a <app>`, piping in only the lines for `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and whichever of `JSA_SEARCH_ANTHROPIC_API_KEY`, `PERPLEXITY_API_KEY`, `GEMINI_API_KEY` are set (the README carries the exact filter). The cloud runs only search, so it holds no other Claude credential, and the search key is required there if the schedule uses `claude`.
4. **Inbox (optional; without `[inbox]` no inbox is deployed):**
   - Create a Gmail account used for nothing but the jobs mailbox.
   - In the Google Cloud project `gws` uses, publish the OAuth consent screen to production. In testing status Google expires its tokens after 7 days, which would silently stop the hourly inbox.
   - Authorize `gws` twice and export each credential: as the jobs mailbox, with Gmail's modify scope only; and as the owner, with Drive's per-app file scope (`drive.file`) only, which also lets the Sheets API write the files the app created.
   - With the owner's credential, create the Drive packets folder with `gws` (the per-app scope reaches only files the app created). Move existing packets into it through Drive for Desktop, and set `packets_dir` to its local path.
   - With the same credential, copy the tracker Sheet with `gws` (a copy keeps its formatting and its Status dropdown), and set `tracker_spreadsheet_id` to the copy.
   - `fly apps create <inbox app>`; set `[inbox] app`, `senders`, and `drive_folder_id` in `profile/config.toml`.
   - Stage the inbox app's secrets with `fly secrets import --stage -a <inbox app>`: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, one Claude credential (`CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, never both; an `ANTHROPIC_API_KEY` from a Console workspace with a monthly spend limit is recommended, because it caps what a forged email could cost), `JSA_GWS_CREDENTIALS` (the owner's export), and `JSA_INBOX_GWS_CREDENTIALS` (the jobs mailbox's). The README carries the exact commands.
5. **Smoke test:** `jsa deploy --smoke` (runs one ungated `jsa cron` on a throwaway machine, exits).
6. **Schedule:** `jsa deploy` (creates the `hourly` machines on first run; the search machine self-gates to `search.toml`).
7. **Updates:** `jsa deploy` again, after any code change, an accepted refine proposal (PRD 05), or a change to `config.toml` or `resume.docx` the inbox should use.

**`jsa deploy` (Priority: P0)** — local only; uses the user's `flyctl` session (no deploy token).
1. **Validate before building:** assemble the search prompt and parse `search.toml` exactly as the cloud will (`XC-13`); any missing required fragment, invalid schedule, or malformed runner or verification setting aborts before a build. Model and agent IDs are not checked against a list (`XC-14`) — `--smoke` is where a rejected ID surfaces. Warns (does not abort) on a schedule whose windows leave part of the week unsearched (each search covers the `window_hours` before its day's `run_at`, and hours no window covers are never searched), on a `run_at` after 22:59 (a slipped wake can cross midnight and miss the day, PRD 01), and on a pending refine proposal (it will not ship). It aborts when the search app lacks a secret the schedule needs (read from `fly secrets list`): `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, and the key of each scheduled agent (`PERPLEXITY_API_KEY`, `JSA_SEARCH_ANTHROPIC_API_KEY`, `GEMINI_API_KEY`), because the machine has no other credential to fall back on. When `[inbox]` is set, it also validates everything the inbox needs — `resume.docx`, `tracker_spreadsheet_id`, `[agents.checklist]`, `[agents.redline]`, and `[inbox]` itself — and aborts when the inbox app lacks any of its secrets or holds both Claude credentials, because the CLI prefers the API key and fails the OAuth flow with a 401 (read from `fly secrets list`; deploy never sets one).
2. **Build and push:** `fly deploy --build-only --push --image-label <UTC stamp> -a <app>`.
3. **Swap in place:** find the machine carrying the `hourly` schedule. None → create it (`fly machine run <image> --schedule hourly --vm-memory 1024 --region <region>`). One → `fly machine update <id> --image <image> --vm-memory 1024 --schedule hourly`; re-asserting the schedule on every update means an image swap can never drop it. Every launch of the pushed image (create, update, and the `--smoke` machine) retries for registry lag, because Fly's registry can trail a push by a few seconds and a launch in that window fails with a missing manifest. Whatever the update does to Fly's interval anchor is harmless, because the run time comes from the gate, not the anchor (PRD 01). More than one → error (ambiguous; the user resolves it in Fly).
4. **The inbox machine** (when `[inbox]` is set): the same swap in the inbox app, with `--entrypoint "jsa inbox"`. Every create and update also sets `config.toml` and `resume.docx` as machine files (`--file-local`), so a deploy is how a changed resume or config reaches the inbox; they never enter the build context or the image (`XC-11`). The same image runs in both apps; how it reaches the inbox app's registry is the engineers' call.
- **`--dry-run`:** step 1 plus the list of profile files that would ship, in the image and, when `[inbox]` is set, as the inbox machine's files; no build.
- **`--smoke`:** steps 1–2, then one `jsa cron --ungated` on a `--rm` machine (retrying for registry lag, as in step 3): it skips the time-of-day gate and the daily claim (so it never consumes the day's scheduled run) and runs today's scheduled searches or, when nothing is scheduled today, the next scheduled day's, so every smoke exercises real runners. It costs one day's searches, and its postings and findings are real and land in the shared DB. The scheduled machines are untouched.
- **Never sets secrets** — those stay a user-run step (above).

**CLI surface (Priority: P0)** — one console command, `jsa`:

| Command | Step | Where | Cloud? |
|---|---|---|---|
| `init-db` | — | local (once) + cloud | via image |
| `search --agent --window-hours` | 1–2 | either | called by cron |
| `cron [--ungated]` | 1–2 | Fly (search app) | **yes (search machine's entrypoint)** |
| `add <URL>` | 3 (skips) | local + inbox | via `inbox` |
| `review` | 3 | local | no |
| `refetch [--id/--all/--dry-run]` | recon | local | no |
| `packet [--id/--dry-run]` | 4 head | local | no |
| `generate [--id/--dry-run]` | 4 | local + inbox (one posting) | via `inbox` |
| `track [--id/--dry-run]` | 5 | local + inbox (one posting) | via `inbox` |
| `inbox` | 3–5 (email) | Fly (inbox app) | **yes (inbox machine's entrypoint)** |
| `refine [--dry-run/--accept/--reject]` | learn | local | no |
| `deploy [--dry-run/--smoke]` | ops | local | ships the image |

**Configuration surface (env + `profile/`) (Priority: P0)** — three kinds of input, three homes (`XC-11`).

*Environment — secrets and machine-local settings only* (every one listed, commented, in `.env.example`):
- **Required everywhere:** `TURSO_DATABASE_URL` (raises if unset). `TURSO_AUTH_TOKEN` required for hosted Turso (omit for a `file:` dev URL).
- **Command-specific:** `PERPLEXITY_API_KEY` / `GEMINI_API_KEY` (their runners' searches; validated lazily so other commands run without them); `JSA_SEARCH_ANTHROPIC_API_KEY` (production Claude searches; unset in development, where the runner uses the inherited credential); for the local Claude commands and the inbox's packets, `CLAUDE_CODE_OAUTH_TOKEN` *or* `ANTHROPIC_API_KEY` (the user's choice; read by the SDK's CLI from the inherited env — not by the app's config); on the inbox app only, `JSA_GWS_CREDENTIALS` (the owner's exported `gws` credential, for the tracker and the packets folder; unset locally, where `gws` uses its own login) and `JSA_INBOX_GWS_CREDENTIALS` (the jobs mailbox's, for Gmail).
- **Optional overrides (default):** `JSA_PROFILE_DIR` (`./profile`), `JSA_GWS_BIN` (`gws`), `JSA_FLY_BIN` (`fly`), `JSA_PANDOC_BIN` (`pandoc`), `JSA_GENERATE_WORKERS` (3).

*The profile — everything about the user* (gitignored in full; `profile.example/` is committed with the identical shape and a fictional candidate):

```
profile/
  config.toml             local + inbox machine file
                                  candidate_name (optional), tracker_spreadsheet_id,
                                  packets_dir (default ~/Documents/Job Applications;
                                    the Drive for Desktop path of the packets folder with the inbox),
                                  [fly] app + region,
                                  [inbox] app + senders + drive_folder_id   (optional; PRD 03, 04)
                                  [agents.checklist|redline|refine] model + effort   (XC-14)
  resume.docx             local + inbox machine file
                                  the single base resume                    (PRD 04)
  search/                 SHIPS IN THE FLY IMAGE
    search.toml                 timezone; run_at; [schedule] weekday → ordered (agent, window_hours);
                                [runners.claude] model + effort;
                                [verification] mode (strict | best_effort)  (PRD 01)
    candidate.md, target_roles.md, filters.md,
    positive_signals.md, negative_signals.md, hard_exclusions.md               (PRD 01 slots)
  refine/                 local   a pending refine proposal: rationale + conflict-marked fragments (PRD 05)
```

- **Validated at load:** each TOML file is parsed into a typed config; an unknown key raises (a typo never silently falls back to a default), and a key a command needs but the profile lacks raises naming the file and pointing to `profile.example/`. Requiredness is per command: `tracker_spreadsheet_id` for `track`/`generate`/`refetch`; `resume.docx` for `packet`/`generate`/`refetch`; `[fly]` for `deploy`; `[inbox]` for `inbox`, and for `deploy` when present; each `[agents.*]` for its command; `[runners.claude]` when Claude is scheduled, and `[verification] mode`, for `search`/`cron`/`deploy`. Model and effort values are checked for form only, never against a list of allowed models (`XC-14`); `profile.example/` carries the recommended defaults as comments.
- **The profile is only data.** No profile file is executable or imported as code; the app reads it only through its config loading and prompt assembly (`XC-13`).

**Complete user-setup inventory (Priority: P0)** — the consolidated home; other PRDs reference this:

*Accounts & cloud (one-time):* Turso account + DB; Fly.io account + `fly apps create` + the scheduled machine (created by `jsa deploy`); Perplexity account + key; a Google AI Studio key (if using `gemini`); an Anthropic Console API key for search (if using `claude`); Anthropic auth for the local Claude commands (`claude setup-token` for the OAuth token, or an API key). For the inbox: a Gmail account used only as the jobs mailbox, a second Fly app, and the Google Cloud project's OAuth consent screen published to production.

*Secrets — where each lives:*
- Local `.env`: `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `PERPLEXITY_API_KEY` (if used), `GEMINI_API_KEY` (if used), `JSA_SEARCH_ANTHROPIC_API_KEY` (if used), one of `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`.
- Fly secrets on the search app (`--stage`): `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `PERPLEXITY_API_KEY` (if used), `GEMINI_API_KEY` (if used), `JSA_SEARCH_ANTHROPIC_API_KEY` (if used).
- Fly secrets on the inbox app (`--stage`, if used): `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, one of `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`, `JSA_GWS_CREDENTIALS`, `JSA_INBOX_GWS_CREDENTIALS`.

*Local tools:* Google Chrome (review); Microsoft Word (redline review); the `gws` CLI + `gws auth login` (Sheets — note the testing-status OAuth 7-day token expiry until the consent screen is published); `flyctl` + `fly auth login` (deploy); `uv`; the Claude Code CLI (for local Claude-driven commands); `pandoc` and `typst` (`generate`'s checklist PDF); Google Drive for Desktop (with the inbox: `packets_dir` mirrors the Drive packets folder).

*The profile the user seeds (`XC-11`)* — `cp -r profile.example profile`, then replace the fictional candidate: `config.toml`; the six search fragments and `search.toml`; `resume.docx` (the single base resume). No app prompt needs an edit.

*Outside the profile:* the Google Sheet (Applications tab, A:H header, and the Status dropdown / data-validation applied to all of column H, so appended rows can never run past the pre-formatted range).

*One-time commands:* `uv run jsa init-db`; with the inbox, creating the Drive packets folder and copying the tracker with `gws`; `jsa deploy`.

-----
#### User Experience

**Entry Point & First-Time Experience**
- A fresh clone: `uv sync` → set `.env` → `cp -r profile.example profile` and make it yours → `jsa init-db` → (cloud) the Fly sequence above, ending in `jsa deploy`. `README.md` ("Using this for your own search") carries the full ordered walkthrough, and states the portability boundary: the local commands assume macOS (Chrome opened through `open`, the `gws` CLI), an accepted constraint rather than a portability goal.
- Local-only dev: `TURSO_DATABASE_URL=file:dev.db` for a throwaway SQLite file (`XC-2`).

**Core Experience**
- *Cloud:* the search machine wakes hourly; `jsa cron` exits within seconds unless it is a scheduled day, at or after `run_at`, and the day is unclaimed — in which case it claims the day, runs the searches, writes to Turso, and stops. The inbox machine wakes hourly too; `jsa inbox` exits within seconds when the jobs mailbox's Inbox is empty, and otherwise works through it, oldest first, then stops.
- *Local:* the human works the pipeline — `review` → `generate` → (auto) `track`, then the user's own revision of each packet's resume copy against its checklist, with `refetch`/`refine` as periodic maintenance; an accepted refine proposal or a code change reaches the cloud only via `jsa deploy`.

**Edge Cases**
- **256 MB machine:** the CLI subprocess hangs — always pass `--vm-memory 1024`.
- **Both Claude credentials set in local `.env`:** the API key wins and OAuth 401s — provide exactly one. (`JSA_SEARCH_ANTHROPIC_API_KEY` doesn't count: the CLI never reads it directly.)
- **`gws` token expired (exit code 2):** re-run `gws auth login` (until the consent screen is published).
- **Missing `TURSO_DATABASE_URL`:** every command raises a clear pointer to `.env.example`.
- **A secret the schedule needs is missing on Fly:** `jsa deploy` aborts naming it, before any build. Left undetected, the scheduled search would fail on the machine (a `claude` search with `Not logged in`).
- **Missing or invalid profile file / key:** the command that needs it raises, naming the file and pointing to `profile.example/`.
- **Schedule dropped after an image update:** cannot persist — `jsa deploy` re-asserts `--schedule hourly` on every update.
- **More than one `hourly` machine in an app:** `jsa deploy` refuses rather than guess which to update.
- **An inbox wake still running at the next hour:** Fly doesn't start a machine that is already running, so wakes never overlap.
- **The inbox's resume or config is stale:** the machine holds what the last `jsa deploy` set; deploy again after changing either.
- **An inbox credential expired or revoked:** every wake fails before it reads mail, and the messages wait unlabeled in the Inbox (PRD 03); `fly logs -a <inbox app>` names the failure.
- **A deploy or manual start wakes the machine mid-day:** the gate decides as on any wake; it cannot cause a second run or move the next one.

-----
#### Technical Considerations
- **No `fly deploy` release lifecycle:** machines run images directly; updates are in-place image swaps that preserve the schedule.
- **Fly's schedule carries no time of day:** intervals are counted from the machine's creation, and whether an update or manual start resets that count is undocumented. Hourly wakes plus the in-app gate make the run time independent of it. The cost is about 24 short wakes a day, each billed per second on the 1 GB machine and exiting before any DB or model call on all but one of them.
- **One timezone, from the profile:** the cadence gate, the search window, and the tracker's Date Added all use `timezone` from `profile/search/search.toml`, never the image's `TZ` or the host's zone, so cloud and local agree on what "today" is.
- **Claude auth is inherited, except for search (`XC-1`):** the local Claude commands' spawned CLI reads the user's credential from the inherited environment, which is why it is not on `Config`. The Claude search runner alone overrides it with `JSA_SEARCH_ANTHROPIC_API_KEY` (PRD 01), when that key is set, so every production search run, cloud or hand, bills to an API key; development leaves it unset and inherits like the rest.
- **`turso_serverless` over HTTP (`XC-2`, PRD 02)** is what makes the same DB reachable identically from Fly and locally.
- **No inbound surface:** both machines only make outbound calls. The jobs mailbox is the inbox machine's queue, so there is no endpoint, token, or run table to secure.
- **Two Google credentials, each scoped to its job:** a credential that can read a mailbox can read password resets. So the inbox reads a mailbox used for nothing else, and the owner's credential reaches only the files the app created: the packets folder and the tracker.
- **Inputs are re-asserted, not synced:** `config.toml` and `resume.docx` reach the inbox machine only through `jsa deploy`, as its image and schedule do; no sync process runs.

-----
#### Integration Points
- **Fly.io** (`flyctl`, the user's local `fly auth login` session) — hosting + scheduler; driven by `jsa deploy`.
- **Turso** (PRD 02).
- **Perplexity / Gemini / Anthropic** APIs (PRD 01).
- **Google Workspace** via the `gws` CLI: Sheets (PRD 04) locally and on the inbox machine; Gmail (PRD 03) and Drive (PRD 04) on the inbox machine.
- **Chrome, `uv`, Node/Claude Code CLI** — local/image tooling.

-----
#### Outstanding Questions
- None open.
