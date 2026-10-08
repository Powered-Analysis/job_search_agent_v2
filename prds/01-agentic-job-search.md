# Agentic Job Search
#### tl;dr

A headless, recurring web-research agent that finds recent, *verifiably open* job postings matching the user's target roles, returns them as a strict `postings` JSON contract, and idempotently captures each one — canonical URL, full job description — into the shared database. It runs unattended on a fixed cadence in the cloud and is also invocable by hand. Its north star is a review backlog with **no dead links and no duplicates**.

This spec owns the search-and-capture pipeline (PRD Steps 1–2): scheduling, the search prompt contract, the three runners, the output schema, URL canonicalization, and ATS resolution + full-JD capture.

------
#### Goals

##### Business Goals
- **Zero dead links reach review:** the pipeline itself checks every emitted posting before insert and drops what it cannot show is open, whichever runner or model produced it, then re-checks each stored posting when review starts and before its packet is built, because postings close while they wait. Under `strict` that check is the employer's own supported-ATS index; `best_effort` widens the search to other platforms and accepts a weaker page check for them (`XC-5`). Dead links cost real human review time and are the primary failure mode this pipeline exists to prevent.
- **Idempotent re-runs:** overlapping search windows, retried crons, and hand runs never create duplicate rows — dedup is exactly one mechanism (URL canonicalization + a `UNIQUE` constraint), so a re-run is always safe (see `XC-3`).
- **Bounded per-run cost:** each run is a single deep-research call (streamed, with a wall-clock ceiling) plus best-effort ATS fetches; cost and wall time are observable in the run log (turns, seconds, USD).
- **Capture completeness:** full job descriptions are captured for genuinely-new postings from all four supported ATS platforms, and from other platforms' pages wherever they carry schema.org `JobPosting` data, so downstream steps (review, the resume checklist) never have to re-fetch.

##### User Goals
- As the job seeker, I get a continuously refreshed, de-duplicated, liveness-verified list of in-scope roles delivered into my review backlog with no effort on my part.
- The roles respect my non-negotiable filters (e.g., location, industry, company type, salary floor, seniority band) while erring toward recall on *fit* so borderline roles still reach me.
- Each posting arrives with its full job description already stored, so review and packet generation are fast.

##### Non-Goals
- **Fit decisioning** (Apply/Skip and feedback) — owned by *Fit Review & Decisioning* (PRD 03). The search agent judges *fit recall-first* and *liveness as a hard gate*, but never decides Apply/Skip.
- **Storage schema, the idempotency constraint, and search telemetry tables** — owned by *Data & Storage* (PRD 02). This spec produces the canonical URL and the values to store; PRD 02 owns the table, the `UNIQUE(canonical_url)` guard, and `search_findings`.
- **Resume/packet/tracker outputs** (PRD 04) and **prompt refinement** (PRD 05).
- **A second dedup stage or embeddings** — deliberately removed; do not reintroduce (`XC-3`).
- **Per-platform verification beyond the four ATS** — Workday, Notion, custom careers sites, etc. get no platform-specific check; under `best_effort` they get only the generic page check (`XC-5`).

-----
#### User Stories

**Job seeker**
- As the job seeker, I want the agent to run on its own several times a week, so that new roles accumulate without me searching manually.
- As the job seeker, I want every surfaced posting to be a real, open role I can actually apply to, so that I don't waste review time on dead links.
- As the job seeker, I want borderline-fit roles included, so that the final call stays mine rather than being pre-filtered away.

**Operator (runs/monitors the cloud cron)**
- As the operator, I want the cloud machine to gate itself to my weekly schedule and time of day, so that I configure one Fly schedule, never pass per-day arguments, and never have to re-establish a run time that Fly's scheduler cannot hold.
- As the operator, I want a live progress trace (queries, fetches, turns, wall time, USD cost) in `fly logs`, so that I can tell a multi-minute run is progressing and spot misdirection or errors.
- As the operator, I want a run to degrade gracefully — a failed JD fetch, an unresolvable URL, or one malformed posting never aborts the run or drops the other postings.
- As the operator, I want a failed, dead, or missed scheduled search to show up the next time I review, so that I learn about it without reading `fly logs`.

**Developer**
- As the developer, I want to invoke exactly the same pipeline by hand with a chosen agent and window (`jsa search --agent … --window-hours …`), so that I can reproduce and debug a cron run locally.
- As the developer, I want unparseable model output to raise loudly, and an invalid posting to be dropped and counted, so that a contract violation is caught immediately and garbage is never inserted.
- As the developer, I want the pure logic (canonicalization, naming, output parsing, ATS resolution) to be I/O-free, so that it is testable in isolation (`XC-9`).

-----
#### Functional Requirements

**Scheduling & cadence (Priority: P0)**
- **Cadence is user config (`XC-11`).** `profile/search/search.toml` declares a `timezone` (IANA name), a `run_at` time of day (`"HH:MM"`, 24-hour, in that timezone), and a `[schedule]` table mapping each weekday to the ordered `(agent, window_hours)` searches to run; days it omits run nothing. It is parsed and validated at load (unknown agent, non-positive window, a malformed `run_at`, a scheduled agent with no `[runners.*]` entry where one is required, or unknown key raises).
- **Self-gating hourly cron.** Fly's scheduler offers only fuzzy intervals counted from the machine's creation: no time of day, no weekday selector, no per-run args. So the machine wakes `hourly` and `jsa cron` decides whether to run:
  1. **Gate (pure, no I/O):** the gate returns the day's ordered searches only when today's weekday is scheduled and `now` is at or after `run_at`; otherwise `jsa cron` exits within seconds, before connecting to the DB. Weekday and time are judged in the profile's `timezone` — never the image's `TZ` or the host clock's zone.
  2. **Claim:** a due wake claims the day (an idempotent insert into `cron_runs`, PRD 02). Only the wake that wins the claim runs the searches; every later wake that day exits.
  3. **Run:** the claimed day's searches run in order. One shared `now` per day means every search that day lands under the same `run_date`. A search that fails is logged and the next one still runs; `jsa cron` exits non-zero if any failed.
- **What the gate buys:** searches start within about an hour after `run_at` wherever Fly's interval happens to be anchored, so no deploy, manual start, or machine update can move the run time. A wake that misses the target hour is caught by the next, because the test is "at or after", not "during".
- **One attempt per day:** the claim is taken *before* the searches run, so a failed day is not retried hourly at search cost; overlapping windows let the next scheduled day recover it.
- **Hand-invocable (P0).** `jsa search --agent {perplexity|claude|gemini} --window-hours N` runs one search-and-capture cycle through the same pipeline. No defaults (all args required).

**Search prompt contract (Priority: P0)**
- **Single shared prompt, assembled (`XC-13`).** Every runner sends the same prompt: the app's search template assembled with the slots below. The window is rendered as an explicit date range in the profile's `timezone` ("the last N hours (from … through …)") so the model judges recency against concrete dates, not a relative phrase.

  | Slot | Source | Required | What the template places around it (precedence) |
  |---|---|---|---|
  | `{{SEARCH_WINDOW}}` | run-time window | yes | The app-owned search-window filter. |
  | `{{LIVENESS_RULES}}` | the app's liveness rules for the `[verification] mode` (one set per mode) | yes | App-owned, never user content: the Liveness and verifiability rules for the mode. |
  | `{{CANDIDATE}}` | `profile/search/candidate.md` | yes | Who the candidate is — context for judging fit; states no rules. |
  | `{{TARGET_ROLES}}` | `profile/search/target_roles.md` | yes | Title seeds and role scope; the template adds "variants and blended titles are in scope" and applies recall-first. |
  | `{{FILTERS}}` | `profile/search/filters.md` | yes | Hard, company- or posting-level tests; subordinate to liveness and the output contract. |
  | `{{POSITIVE_SIGNALS}}` | `profile/search/positive_signals.md` | no | Query seeds and in-scope confirmation; absence never excludes. |
  | `{{NEGATIVE_SIGNALS}}` | `profile/search/negative_signals.md` | no | Steer verification effort only; never exclude. |
  | `{{HARD_EXCLUSIONS}}` | `profile/search/hard_exclusions.md` | no | The only content-based drops; judged on the title alone. |

- **Two output standards, never conflated (P0):** *role fit* is judged **recall-first** (catch borderline positives; false negatives on fit are the worst outcome), while *liveness/verifiability* is a **hard gate** (never recall-first).
- **Template-owned fit policy (P0):** the template, not the fragments, defines how each kind of user rule behaves: filters are hard; a filter whose criterion the posting does not state (e.g. no published salary) never excludes; negative signals only order effort; hard exclusions are title-level only; anything else that looks wrong in a posting body is the downstream review's call — include. A fragment supplies which filters, signals, and exclusions exist, never how they operate.
- **Template-owned machinery (P0):** Sources (where to discover postings, and which sources count as evidence), the Output contract, volume and de-duplication guidance, and Liveness and verifiability (the mode's `{{LIVENESS_RULES}}` file) live only in app-owned prompt files. No fragment can alter them, and the template states that they override any fragment that conflicts.
- **Liveness gates (hard, P0):** in both modes the employer's own record is the single source of truth; the posting must be open and accepting applications; recency comes from the employer's page only; and aggregators (LinkedIn, Indeed, and the like) are discovery sources, never evidence and never the emitted URL. A job on one of the four supported ATS is emitted as its ATS URL in the wire contract's form (below), and an orphaned detail page there is a closed job. The modes differ only off the four: `strict` forbids emitting such a job; `best_effort` emits the employer's own posting page once the agent has opened it and confirmed it shows the job open.

**Runner and verification configuration (Priority: P0)** — model and effort are the user's; tools, limits, and how verification works are the app's; which postings verification admits is the user's (`XC-5`, `XC-14`). These settings live in `profile/search/search.toml` beside the schedule, because they ship to the cloud with it. `profile.example/` carries them with these comments, which are part of the spec:

```toml
[runners.claude]
# Any Claude model that accepts an effort setting will run: the pipeline checks every posting's
# liveness itself, so this choice trades cost against the quality of what is found.
# We recommend an Opus model at effort "high" or above. Lower effort makes fewer tool
# calls, which shows up as more postings dropped `not_on_index` in your run summaries.
model  = "claude-opus-5-5"
effort = "high"

# Perplexity and Gemini have no settings: Perplexity always runs the "xhigh" preset, and
# Gemini the "deep-research-preview-04-2026" agent.

[verification]
# Which postings the pipeline admits. Required.
#   "strict"       only Greenhouse, Lever, Ashby, and Rippling postings, each checked
#                  against the employer's job index and the search window.
#   "best_effort"  also postings on any other employer page (Workday, iCIMS, custom
#                  careers sites). The pipeline checks those pages for signs the job
#                  closed, and checks the posted date when the page publishes one, but
#                  a page can stay up after its job closes, so expect some dead links.
#                  Run summaries count these postings as `reachable`/`reachable_no_date`.
mode = "strict"
```

- **Validation:** `model` and `effort` must be non-empty strings, and `effort` one of `low`/`medium`/`high`/`xhigh`/`max`; `mode` must be `strict` or `best_effort`, with no default. The app keeps no list of allowed models — an ID the provider rejects surfaces at `jsa deploy --smoke` or the first run.
- **Effort is always passed explicitly**, never left to a model's default (Opus 5.5 defaults to `medium`).

**Runners (Priority: P0)**
- **Perplexity runner:** calls the **Agent API** `POST /v1/agent` with **`preset = "xhigh"` and no overrides** — no `model`, `max_steps`, or `tools` — so Perplexity's bundle (model, step budget, system prompt, and its `web_search` / `finance_search` / `sandbox` tools) applies exactly as Perplexity defines it; any override would replace part of that bundle. It sends no `service_tier`: the lower-cost `flex` tier serves only OpenAI models, and `xhigh` resolves to an Anthropic one, so the run is on standard capacity at list price. The request carries `input`, streaming, and a `response_format` json_schema mirroring the output contract (enforced at the API layer in addition to the in-prompt contract). It streams typed SSE events and folds them into a pure stream state: sandbox steps (one per `response.sandbox.results` event; `xhigh` does its searching from inside the sandbox), final text (`response.output_text.done` preferred over reassembled deltas), and, from `response.completed`, USD cost and the **model Perplexity reports**. The model is recorded on every finding so a change in what the preset runs is visible in telemetry. Emits a heartbeat at most every 5s, counting sandbox steps; 1800s read timeout. The run is a foreground request, which Perplexity cancels once its client has gone, so the runner closes the stream the moment it stops reading, however it stops.
- **Claude runner:** drives the **Claude Agent SDK** headless through the shared agent loop (`XC-12`), with `model` and `effort` from `[runners.claude]`, no permission prompts, and at most 120 turns. **Its only tools are `WebSearch` and `WebFetch`; every other tool is unavailable, not merely unapproved.** This agent reads untrusted web pages on a machine whose environment holds the app's API keys, and the SDK's allow list does not restrict a run that skips permission prompts — every unlisted tool, `Bash` included, would still run. It emits a live trace (each tool call, model narration/thinking, tool errors, and a closing line with turns/seconds/USD) and returns the SDK's final result string (falling back to concatenated assistant text). **Auth (`XC-1`):** when `JSA_SEARCH_ANTHROPIC_API_KEY` is set, the spawned CLI's only credential is that key, as `ANTHROPIC_API_KEY`. The SDK layers a run's environment over the inherited one, so an OAuth token in local `.env` (used by PRDs 04/05) must be actively cleared for that run, not just left out. When it is unset, the runner passes no override and the CLI reads the inherited Claude credential, like every other Claude call. Production (the Fly secret and the owner's local `.env`) sets it, so production searches bill to an API key; development (CI, the coding team) leaves it unset and needs no API key. How the token is cleared is the engineers' call; a live check at implementation proves the CLI authenticates with the API key alone.
- **Gemini runner:** drives the **Gemini Deep Research agent** through the **Interactions API** (the only surface that serves it; `google-genai` SDK, `client.interactions.create`).
  - **Request:** `agent = "deep-research-preview-04-2026"` (pinned, `XC-14`), `background = True`, `stream = True`, the assembled prompt as `input`. `agent_config = {type: deep-research, thinking_summaries: auto, visualization: off, collaborative_planning: false}`: summaries on because the stream carries no progress without them; visualization off because only text is consumed; no collaborative planning because the run is headless. Explicit `tools = [google_search, url_context]`, with no code execution and no MCP.
  - **Output:** the agent has **no server-side structured output**, so the JSON contract is enforced in the prompt and by output parsing alone, exactly as for Claude.
  - **Stream folding:** events fold into a pure stream state, as for Perplexity. It counts `step.delta` thought summaries and logs each one's text, clipped to a line, as it arrives (the agent's own account of what it is doing, and the only live trace the stream offers), collects the final text (from the completed interaction, preferred over reassembled `text` deltas), and records the interaction id and the latest `event_id`. Heartbeat at most every 5s; the closing line reports steps, seconds, and cost.
  - **Reconnects:** Interactions streams drop at about 600s, so a dropped stream reconnects with `GET /interactions/{id}?stream=true&last_event_id=…` (status polling as the last resort). Only an `interaction.error` / `failed` status or the wall-clock ceiling ends the run early.
  - **Cancellation:** a background interaction keeps working, and billing, after its client has gone. The runner logs the interaction id as soon as the stream reports it, and whenever a run ends before the interaction has (the ceiling, an error, an interrupt) it cancels the interaction with `POST /interactions/{id}/cancel`. A cancel that fails is logged with the id, so the interaction can be cancelled by hand.
  - **Cost:** reported as tokens from the interaction's `usage`, with USD **estimated** from pinned per-token rates and labeled as an estimate. The API returns no billed amount, and missing `usage` logs "cost unknown" rather than failing.
  - **Auth:** `GEMINI_API_KEY`, validated lazily like `PERPLEXITY_API_KEY`.
- **Every runner reports its resolved model and effort** (Perplexity: the reported model, no effort; Gemini: the agent ID, no effort) with its output, for the findings telemetry.

**Output contract & parsing (Priority: P0)**
- **Wire contract (owned here):** each search agent returns a single JSON object `{"postings": [ … ]}` where each posting has `company` (required), `title` (required, the exact posting title), `url` (**required, hard gate** — the posting's URL on its ATS: Greenhouse `https://job-boards.greenhouse.io/{token}/jobs/{id}`, built from the board token and job id; Lever `hostedUrl`, Ashby `jobUrl`, and Rippling `url` as the index returns them; under `best_effort`, for a job off the four, the employer's own posting page), and `date_posted` (optional ISO-8601, only when anchored to an explicit date/"N days ago" on the employer's page; never guessed). Greenhouse's index `absolute_url` is never the emitted URL: it often points at the employer's own site, sometimes at a search page that differs per job only by `gh_jid`, and it carries no board token, so the pipeline could not check it. The `job-boards` form serves every board and redirects to the employer's page where one exists.
- **Validation:** parsing tolerantly extracts the outermost JSON value (stripping markdown fences and stray prose — Claude and Gemini have no server-side structured output), accepts either the full object or a bare array (wrapped). A response with no extractable JSON value, or no `postings` array, **raises** after writing the raw final text to the log, so the violation can be diagnosed from `fly logs`. Each posting is then validated on its own against the contract (pydantic is approved for this). An invalid posting (a missing or non-string required field) is dropped, logged with its raw entry, and counted `malformed`, so one bad entry never discards a paid run and nothing invalid is ever inserted; an invalid optional `date_posted` is discarded, not the posting. A URL emitted more than once in one response is processed once, at its first rank.

**Pipeline verification: liveness and recency (Priority: P0, `XC-5`)**
- **The pipeline, not the agent, is the guarantee.** Before any insert, each emitted posting is checked against its employer's own record: that it is open and that it falls inside the search window. On the four supported ATS that record is the board's index; off them, under `best_effort`, it is the posting page itself. The agent's in-prompt liveness and recency work remains, because it keeps the agent's effort and output on live, in-window postings. But nothing an agent reports about its own checks is trusted, so the guarantee holds identically for every runner, model, and preset. Both checks are insurance on agent work, and both drop: a dead link can never become an application, and an out-of-window find is, in the user's review history, almost always a Skip.
- **Recency timestamp per platform** (the recency column of the table below). Greenhouse `updated_at` (last modified), Lever `createdAt` (created; undocumented by Lever), Ashby `publishedAt` (last published), all from the index fetch already made; Rippling `createdOn` from the per-job detail endpoint, fetched only for Rippling postings and reused for capture. The four measure different events, all compatible with the prompt's rule "published or updated within the window". The known gap is Greenhouse: any edit resets `updated_at`, so an old posting edited recently passes. The check can let a stale posting through but never drops a fresh one.
- **Window test:** the posting passes when its timestamp is no earlier than the window start minus **24 hours of slack** (absorbing timezone and clock skew between agent, ATS, and pipeline; overlapping windows make a wider margin unnecessary).
- **Page check (`best_effort`, off the four):** one GET of the emitted URL, following redirects. Closed signals: HTTP 404 or 410; a redirect whose final URL no longer contains the requested URL's last path segment (typically the careers home or a search page); a schema.org `JobPosting` `validThrough` in the past. Recency: the page's `JobPosting` `datePosted` (JSON-LD or microdata), run through the same window test and recorded as kind `published`. The fetched page is kept for capture. **Known gap:** a site may answer a closed or nonexistent job with HTTP 200 and no `JobPosting` data — Workday does — and that page passes as `reachable_no_date`. The dead-link rate of `reachable`/`reachable_no_date` findings, read against `verified` ones in `search_findings`, is what shows whether `best_effort` earns its place.
- **Outcomes** (`verification`), checked in this order:
  - `aggregator` — the URL's host is on the app-owned aggregator list, so it is not the employer's record (a prompt violation, in both modes). Dropped. The list is necessarily incomplete: an aggregator it misses is page-checked like any other host under `best_effort` and is visible by host in `search_findings`.
  - `unsupported` — `strict` only: the URL resolves to none of the four ATS, so there is no index to check (a prompt violation). Dropped.
  - `unverifiable` — a fetch the check needs (the index, Rippling's detail, or the posting page) failed: timeout, a non-2xx status that is not a closed signal (a 403 or 429 is a block, not a closure), unparseable. Dropped, labeled separately so an outage is distinguishable from an agent error; overlapping windows usually let a later run re-find the posting.
  - `not_on_index` — the index was fetched and the job id is absent: closed, orphaned, or invented. Dropped.
  - `page_closed` — `best_effort`, off the four: the page check found a closed signal. Dropped.
  - `out_of_window` — open, but its timestamp (the ATS's, or the page's `datePosted`) is before the window start minus the slack. Dropped.
  - `verified` — open and in window. Inserted.
  - `verified_no_date` — open, but the platform returned no usable timestamp. **Inserted**, and the run summary warns naming the platform. A missing field is structural, not transient: if Lever dropped its undocumented `createdAt`, failing closed would silently remove every Lever posting from then on.
  - `reachable` — `best_effort`, off the four: no closed signal, and `datePosted` in window. Inserted.
  - `reachable_no_date` — the same, but the page publishes no `datePosted`, so recency rests on the agent's claim. **Inserted**, and the run summary warns with the count.
- **Re-check of stored postings (P0, `XC-5`):** the same check, minus the window test, runs again wherever a human is about to spend time on a stored posting: at the start of `jsa review` (PRD 03) and before `jsa generate` builds a packet (PRD 04). A stored posting passed the window at insert, so only liveness matters now. Each posting gets its own platform's check (the index on the four, the page check otherwise) whatever the current `verification.mode`, with one index fetch per board per session and no model call. Only a definite closed signal (`not_on_index`, `page_closed`) marks the posting closed (`closed_at`, PRD 02); `unverifiable` leaves it as it was. A closed posting gets no decision, so refine (PRD 05) never mistakes it for a fit judgment. Closed is final: a later search that re-finds the posting no-ops on insert as before. The re-check writes no `search_findings` row, since that table is per-agent search telemetry.
- **One index fetch per board per run:** `(platform, board)` indexes are fetched once and reused for every posting on that board in the run. Ashby's index is also its capture source, so the cached board serves both.
- **Deterministic core:** classifying a posting — from the mode, its resolved ATS, the fetched index, detail, and page results, and the window, to its `verification`, `ats_date`, and `ats_date_kind` — is pure (`XC-9`); fetching is separate.

**Idempotent capture pipeline (Priority: P0)**
- **Per-run body,** for each returned posting in rank order:
  1. Canonicalize the URL (owned here) → the canonical idempotency key.
  2. Resolve the ATS, then verify → a `verification` outcome and the ATS timestamp.
  3. Record the finding — log **every** emitted posting, with its `verification`, ATS timestamp and kind, and the run's resolved model and effort, *before* any insert, so a req the insert no-ops (already found by another agent/run) still credits this agent and a dropped posting still counts against it (table owned by PRD 02).
  4. **Admitted outcomes only** (`verified`, `verified_no_date`, `reachable`, `reachable_no_date`): the idempotent insert (PRD 02), which reports a new id or that the row was already present.
  5. **New rows only:** capture the JD and store it (PRD 02). A failed capture is caught, counted, logged, and leaves `jd_markdown` NULL — **never excludes the row** (`XC-6`); the row's liveness and recency were already established in step 2. (Rippling's capture reuses the detail record fetched for verification, and an off-four posting's capture reuses its fetched page.)
- **URL canonicalization (owned here):** lowercase scheme+host, strip a denylisted set of tracking/session query params (and `utm_*` prefixes), drop the fragment, normalize the trailing slash; path case preserved (ATS job IDs/slugs are case-sensitive). Greenhouse URLs on either board host (`boards.greenhouse.io`, `job-boards.greenhouse.io`) canonicalize to the `job-boards` form. A query parameter that identifies the job (e.g. Greenhouse's `gh_jid`) is never on the denylist. Pure, I/O-free. This is the *sole* insert-idempotency mechanism and also carries cross-source dedup, because agents converge on the same employer URL for a req (`XC-3`).
- **Filesystem-safe naming (owned here):** the normalized company (title-case, strip corporate suffixes, remove path-hostile chars) and the title slug (path-safe, ≤80 chars) are computed once at insert so downstream packet paths need no re-sanitizing. Pure, I/O-free.

**ATS resolution & full-JD capture (Priority: P0)**
- **Four supported ATS = the platforms whose liveness the pipeline can check (`XC-5`):** Greenhouse, Lever, Ashby, Rippling. Resolution maps a posting URL to `(platform, board/token/slug/org, id)` by host and path; a URL resolving to none of them gets verification `unsupported` under `strict` and the page check under `best_effort`. A Greenhouse posting on the employer's own domain (`?gh_jid=` with no board token) does not resolve — it is a prompt violation, since the wire contract requires the `job-boards` form.
- **Capture (owned here):** GET the ATS detail record (User-Agent `job-search-agent/<package version>`, carrying no personal identifier), convert HTML→Markdown (ATX headings; `markdownify` is approved for this), and store `jd_markdown`, structured `location` (normalized from string/dict/list), and the ATS-canonical `title` (which overwrites the agent's transcription and re-derives `title_slug` — see PRD 02's JD capture). This fetcher and its per-platform shapes are the single home for ATS capture; PRD 03 (manual add) and PRD 04 (refetch) reuse it.

  | Platform | Index fetch (liveness) | Recency timestamp | Detail fetch | JD field | Title | Location | Quirk |
  |---|---|---|---|---|---|---|---|
  | Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs` | index `updated_at` (last modified) | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` | `content` (entity-escaped — unescape) | `title` | `location.name` | — |
  | Lever | `GET api.lever.co/v0/postings/{slug}?mode=json` | index `createdAt`, epoch ms (created; undocumented) | `GET api.lever.co/v0/postings/{slug}/{id}?mode=json` | `description` → `descriptionPlain` | `text` | `categories.location` | on HTTP error retry host `api.eu.lever.co` (both fetches) |
  | Ashby | `GET api.ashbyhq.com/posting-api/job-board/{org}` | index `publishedAt` (last published) | (the same board) | `descriptionHtml` | `title` | `location` | no per-id GET — scan the org board for the UUID |
  | Rippling | the paginated `ats.rippling.com/api/v2/board/{board}/jobs` list | detail `createdOn` (created; undocumented) | `GET ats.rippling.com/api/v2/board/{board}/jobs/{id}` | `description`, a `{role, company}` HTML dict (role then company) | `name` → `title` | `workLocations`/`locations` | list items carry no JD and no timestamp — the detail record is the only source of both |
- **`JobPosting` fallback for pages off the four (P0, `XC-5`):** the extractor reads schema.org `JobPosting` data from a page's HTML — JSON-LD or microdata, since sites use both (Workday embeds JSON-LD; SmartRecruiters marks up microdata) — yielding `description` (→ `jd_markdown`, through the same HTML→Markdown conversion), `title`, `hiringOrganization` name, `jobLocation`, `datePosted`, and `validThrough`. It is pure over the HTML string (`XC-9`). Capture order everywhere is **supported fetcher → `JobPosting` data → `NULL`**, except that the email side door puts a description the email carries before `NULL` (PRD 03); Greenhouse and Lever publish no `JobPosting` data, so their fetchers are the only source. The fallback serves every path that admits a posting off the four: search under `best_effort` (on the page already fetched for verification), manual add (PRD 03), and refetch (PRD 04). Under `strict` no searched posting off the four reaches capture. Capture and liveness stay separate (`XC-6`): verification decides liveness, reading `datePosted` and `validThrough` for it; a successful capture is never itself evidence a posting is live, and a failed one never excludes an admitted row.

**Run telemetry (Priority: P1)**
- Each run produces a run summary (`agent`, `model`, `effort`, `mode`, `cost`, `found`, `verified`, `verified_no_date`, `reachable`, `reachable_no_date`, `aggregator`, `unsupported`, `unverifiable`, `not_on_index`, `page_closed`, `out_of_window`, `malformed`, `inserted`, `already_present`, `jd_captured`, `fetch_failed`, `errors`) logged as the closing line and echoed by the CLI. Every search, whether scheduled, hand, or smoke, opens its `search_runs` row before the runner starts and closes it with the outcome, the summary, and its warnings when the search ends (PRD 02). That row is what `jsa review`'s health line reads (PRD 03), so the summary reaches the user without `fly logs`. The verification counts are the user's direct read on whether a model/effort choice is emitting dead links or straying out of window. Any `verified_no_date` adds a closing warning naming the platform, because a platform that stops returning dates has silently lost its recency check. Any `reachable_no_date` adds a closing warning with the count, because those postings' recency is the agent's word. Any `malformed` adds a closing warning with the count, because a runner that keeps breaking the contract is losing finds.

-----
#### User Experience

**Entry Point & First-Time Experience**
- *Cloud:* the Fly machine runs `jsa cron` on an `hourly` schedule; the first wake at or after `run_at` on a scheduled weekday claims the day and runs its ordered searches, and every other wake prints why it is not running ("no search scheduled today", "before run_at", "already ran today") and exits 0. First-time setup (Fly app, secrets, schedule) is owned by PRD 06.
- *Local/dev:* `jsa search --agent … --window-hours …` runs one cycle against the same DB. Every command ensures the schema itself (PRD 02).

**Core Experience**
1. Assemble the search prompt (`XC-13`) from the template, the `profile/search/` fragments, and the date-range window in the profile's timezone.
2. Open the search's `search_runs` row (PRD 02), then run the selected runner; watch the streamed/logged progress trace.
3. Parse the returned JSON and validate each posting, dropping invalid ones as `malformed`.
4. Connect to the DB (autocommit per `XC-8`), ensure the schema, then per posting: canonicalize → resolve + verify liveness and recency against the employer's record → record finding → (admitted only) idempotent insert → (new only) fetch + store JD.
5. Print the run summary and close the `search_runs` row.

**Edge Cases**
- **Missing or empty required search fragment, or an invalid `search.toml`:** raises before any model call, naming the file and pointing to `profile.example/search/`.
- **Unparseable model output:** the raw text is logged, then parsing raises (no silent garbage insert). An empty `postings` array is valid and yields a zero-insert run.
- **An invalid posting among valid ones:** dropped, logged with its raw entry, and counted `malformed`; the rest of the run proceeds, and the summary warns.
- **Process killed mid-search** (a stopped machine, an out-of-memory kill): its `search_runs` row is left with no outcome, and review's health line reports it as dead (PRD 03).
- **A stored posting closes before review or packet building:** the re-check marks it closed; it leaves the backlog, or the packet queue, without a decision.
- **Same URL twice in one response:** processed once, at its first rank.
- **Runner hits its wall-clock ceiling:** raises; nothing from that search is inserted.
- **One of the day's searches fails:** logged; the day's remaining searches still run, and `jsa cron` exits non-zero.
- **JD fetch fails / times out:** caught per posting, counted `fetch_failed`, error appended, row kept with NULL `jd_markdown`.
- **URL on an aggregator:** dropped as `aggregator` in both modes; the finding is recorded.
- **URL resolves to no supported ATS:** under `strict`, dropped as `unsupported`; the finding is recorded, nothing is inserted. Under `best_effort`, page-checked.
- **Off-four page shows a closed signal** (404/410, redirect away from the posting, past `validThrough`): dropped as `page_closed`; the finding is recorded.
- **Off-four page with no `datePosted`:** inserted as `reachable_no_date`; the run summary warns with the count.
- **Job id absent from its board's index:** dropped as `not_on_index`; the finding is recorded.
- **Index fetch fails (timeout, 5xx, unparseable):** every posting on that board is dropped as `unverifiable` and recorded; a later overlapping run re-checks.
- **Open but older than the window (minus 24h slack):** dropped as `out_of_window`; the finding is recorded.
- **Platform returns no usable timestamp:** inserted as `verified_no_date`; the run summary warns naming the platform.
- **Already-present posting:** still verified (the finding records whether it is live today); if verified, insert returns `None`, counted `already_present`, no capture attempted.
- **Skipped/failed cron fire:** recovered by the next run because windows overlap and re-inserts no-op on `canonical_url`.
- **`run_at` in the day's last hour:** a wake that slips past midnight lands on the next date, so the day can be missed; `jsa deploy` warns on a `run_at` after 22:59.
- **Hand runs (`jsa search`)** neither check the gate nor take the claim, so they never consume or block a scheduled day.
- **Missing `PERPLEXITY_API_KEY` / `GEMINI_API_KEY` on that runner's run:** raises a clear `RuntimeError` before any model call.
- **Gemini stream drops mid-run:** reconnects from the last `event_id`; the interaction keeps running server-side in the background, so a drop loses no work.
- **Thin window (<~5 companies):** the prompt instructs broadening *discovery* (more titles/sources/variants) before concluding the window is empty — never relaxing liveness/window/filters.

-----
#### Technical Considerations
- **Cloud-only subsystem (`XC-1`):** Steps 1–2 run headless on Fly.io; this spec never touches the local disk, Chrome, or Google OAuth.
- **Shared hosted DB, autocommit (`XC-2`, `XC-8`):** writes go to the one Turso database via `turso_serverless`; the connection must set `isolation_level = None` or every write is silently rolled back at `close()`. Owned by PRD 02; load-bearing here because the pipeline inserts depend on it.
- **Streaming as liveness-of-run:** every runner streams because a deep-research call is a multi-minute black box; the trace is the evidence of progress and the first place errors surface, identically in local logs and `fly logs`.
- **Model and effort are the user's (`XC-14`):** the app's opinions are limited to tool calling — each runner's tool set, turn/time limits, stream handling, the output contract, and pipeline verification. Perplexity and Gemini are the exceptions by the user's choice. Perplexity is pinned wholesale to the `xhigh` preset, and the model the preset resolves to is recorded rather than configured. Gemini is pinned to the `deep-research-preview-04-2026` agent, whose id is recorded as its model: the only other Deep Research agent, `deep-research-max-preview-04-2026`, runs past the wall-clock ceiling on this search, at many times the cost, and streams only a few fixed phase labels while it works.
- **What the runners actually differ in:** Perplexity's Agent API runs a third-party model (per its preset table, `xhigh` is currently `anthropic/claude-opus-5-5` at `high` effort) inside Perplexity's search index, sandbox, and system prompt. With the example defaults, the Perplexity and Claude runners use the same model; their difference is retrieval infrastructure and harness. Whether each earns its place is measured, not assumed: `search_findings` gives each runner's unique verified finds and dead-link rate, and the run summaries give cost.
- **Timeouts/limits:** every runner has a 3600s wall-clock ceiling, a hard stop that raises and inserts nothing, so a hung run (a stalled stream, a hung CLI subprocess) can neither bill the machine indefinitely nor block the day's later searches. **Ending the app's wait also ends the provider's work**, or the ceiling would cap nothing: Claude's CLI subprocess ends with the agent loop, Perplexity's stream is closed, and Gemini's interaction is cancelled. Within it: Perplexity has the preset's own step budget and an 1800s read timeout (streaming keeps the connection alive); Claude has 120 turns for many-source research with per-posting index checks; Gemini's ceiling spans reconnects, since a background interaction has no turn cap of its own.
- **Verification is cheap by construction:** plain unauthenticated requests, one index per board per run plus one detail per Rippling posting and, under `best_effort`, one page per posting off the four, so it adds seconds to a multi-minute run.
- **Dedup off the four (`best_effort`):** an employer page reachable under several URLs (a locale prefix, a careers-site alias in front of the ATS) can yield duplicate rows that canonicalization does not merge. `search_findings` shows how often.
- **Two platforms' timestamps are undocumented** (Lever `createdAt`, Rippling `createdOn`). The fail-open `verified_no_date` path and its warning are what keep a silent API change from either dropping a platform wholesale or disabling its recency check unnoticed.

-----
#### Integration Points
- **Perplexity Agent API** — `POST https://api.perplexity.ai/v1/agent`, `xhigh` preset with no overrides, streamed SSE, `response_format` json_schema. Auth: `PERPLEXITY_API_KEY` (Fly secret / local `.env`). Called via `httpx`.
- **Gemini Interactions API** — `google-genai` `client.interactions.create` with the `deep-research-preview-04-2026` agent, `background` + `stream`, stream resumption via `last_event_id`. Tools: `google_search`, `url_context`. Auth: `GEMINI_API_KEY` (Fly secret / local `.env`).
- **Claude Agent SDK** — spawns the Claude Code CLI subprocess (bundled with the SDK), authenticated as `ANTHROPIC_API_KEY` from `JSA_SEARCH_ANTHROPIC_API_KEY` via the runner's `env` override when that key is set, otherwise from the inherited Claude credential. Tools: `WebSearch`, `WebFetch`.
- **Four ATS JSON index/detail endpoints** — Greenhouse `boards-api.greenhouse.io`, Lever `api.lever.co` (EU fallback `api.eu.lever.co`), Ashby `api.ashbyhq.com/posting-api`, Rippling `ats.rippling.com/api/v2`. Plain unauthenticated requests via `httpx` (follow redirects); shapes in the table above.
- **Employer posting pages off the four** (`best_effort`) — one plain GET per posting via `httpx` (follow redirects); schema.org `JobPosting` data parsed from the HTML.
- **Turso (libSQL)** — the shared database; connection/contract owned by PRD 02.

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06):
- `PERPLEXITY_API_KEY` — obtain from Perplexity; set as a Fly secret (cloud) and in local `.env`. Needed only if `search.toml` or a hand run uses `perplexity`.
- `GEMINI_API_KEY` — obtain from Google AI Studio; set as a Fly secret and in local `.env`. Needed only if `search.toml` or a hand run uses `gemini`.
- `JSA_SEARCH_ANTHROPIC_API_KEY` — an Anthropic Console API key, production only; set as a Fly secret and in the owner's local `.env`. Needed only if `search.toml` or a hand run uses `claude`; development leaves it unset.
- `TURSO_DATABASE_URL` (+ `TURSO_AUTH_TOKEN`) — the shared DB (PRD 02/06).
- **`profile/search/`** (`XC-11`), seeded from `profile.example/search/`: the six fragments in the slot table above and `search.toml` (timezone, cadence, and runner settings). The fragments stay standalone — no ground-truth references or watermarks (`XC-7`). This is the only profile content that ships to the cloud; `jsa deploy` bakes it into the image (PRD 06).

-----
#### Outstanding Questions
- **Does `response.completed` report the resolved model?** The telemetry design assumes Perplexity returns the model that ran; confirm on the first live run, or record the preset name alone.
