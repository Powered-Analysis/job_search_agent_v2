# Fit Review & Decisioning
#### tl;dr

The human-in-the-loop stage where captured postings become decisions. `jsa review` is a deterministic, **no-LLM** terminal loop that opens each undecided posting in Chrome and records an Apply/Skip decision plus free-text fit feedback — the ground truth the downstream refinement loop depends on. A second, faster path, `jsa add <URL>`, lets the user hand-add a posting they already want: supplying the URL *is* the Apply decision, so it skips review entirely. This spec owns PRD Step 3 and the manual-add side door.

------
#### Goals

##### Business Goals
- **Zero-friction feedback capture:** the fit-feedback field is load-bearing ground truth (PRD 05), so editing it must be frictionless — arrow keys, word delete, and a *pre-filled editable buffer* on amend. Friction here is a design defect.
- **No token cost per posting:** review is deterministic and uses no model, so working a large backlog costs nothing beyond the human's time.
- **Every decision persists immediately:** each Apply/Skip commits on write (`XC-8`), so an interrupted session loses nothing.
- **A fast lane for known-good roles:** `jsa add` gets a role the user already found straight into the Apply queue without a review turn.

##### User Goals
- As the job seeker, I want to see each undecided role in my browser and record Apply/Skip with one keystroke, so that I can clear a backlog quickly.
- As the job seeker, I want to attach a note explaining *why*, and to amend that note or flip the decision later in the same session, so that my ground truth is accurate.
- As the job seeker, I want to drop in a URL I found myself and have it treated as Apply immediately, so that I don't review a role I've already chosen.

##### Non-Goals
- **Storage of the decision/feedback** — owned by PRD 02 (`record_decision`, `set_decision`, `clear_decision`, `pending_review`).
- **ATS resolution and full-JD capture mechanics** — owned by PRD 01 (`resolve.py`, `fetch.py`); this spec owns only the manual path's *JSON-LD fallback* wiring (`XC-5`).
- **What happens to an Apply row afterward** (packets, resume checklists, tracker) — PRD 04.
- **How feedback is consumed** for prompt refinement — PRD 05.
- **Any LLM in the review loop** — deliberately excluded to keep per-posting cost at zero.

-----
#### User Stories

**Job seeker**
- As the job seeker, I want the backlog presented oldest-first, so that stale postings are triaged before they close.
- As the job seeker, I want to step back to a previous posting and amend my call, so that a too-quick decision is recoverable.
- As the job seeker, I want `jsa add` to derive the company and title for me (and let me correct them) rather than making me type them, so that adding a role is quick.
- As the job seeker, I want re-adding a URL I already reviewed to promote it to Apply while keeping the note I wrote, so that nothing is lost.

**Developer**
- As the developer, I want the review loop to fall back to bare `input()` when there is no TTY, so that it does not crash in a non-interactive environment — while keeping the rich line editing when a terminal is present.
- As the developer, I want the feedback parser (`parse_feedback_entry`) and `company_from_board` to be pure, so that they are testable in isolation (`XC-9`).

-----
#### Functional Requirements

**Review loop (`review.py`) (Priority: P0)**
- **Backlog:** `db.pending_review` returns `decision IS NULL` rows oldest-first; the backlog is captured once at session start so step-back navigation is stable.
- **Open in browser:** each posting opens via `subprocess.run(["open", "-a", "Google Chrome", url])` (macOS; `check=False`, so a missing Chrome fails silently rather than aborting the loop).
- **Decision prompt:** single-letter choices — **`a` = Apply, `s` = Skip, `b` = back, `q` = quit**; a bare Enter (keep the existing decision) is offered only when the posting already has a recorded decision. No pre-filled buffer at the decision prompt (a single-letter pre-fill would double the keystroke).
- **Stored vs displayed:** the keystroke `a`/`s` maps to the stored/displayed decision strings **`Apply`/`Skip`** (`_DECISIONS`).
- **Feedback + amend (P0):** after a decision, prompt `"Feedback (Enter to skip, :a/:s to change the decision): "` through `parse_feedback_entry`. The buffer is **pre-filled with any prior feedback** (`default=entry.feedback or ""`) so amending edits the existing note rather than retyping it. Inline commands (case-insensitive): `:a`/`:apply` and `:s`/`:skip` flip the decision and keep the remaining text as feedback; `:b`/`:back` discards and returns to the decision prompt for the same posting.
- **Revisability (P0):** within a session a posting can be revisited and re-decided (shown as " (amending)"); after the backlog, `_offer_final_amend` gives one more pass at the last entry. Each write refreshes `decided_at` (PRD 02), so amended rows re-enter refinement scope.
- **Persistence:** decisions are written via `db.record_decision` and committed immediately; a Ctrl-C/Ctrl-D is caught as `prompting.Quit` and already-committed decisions survive.
- **No LLM:** the loop makes no model calls.

**Prompt infrastructure (`prompting.py`) (Priority: P1)**
- A `prompt_toolkit` wrapper providing arrow keys, word delete, the pre-filled editable buffer the amend flow depends on, and `Ctrl-X Ctrl-E` → `$EDITOR` for long notes. It **degrades to bare `input()` (with `readline`)** when `prompt_toolkit` is unavailable or stdin/stdout is not a TTY. `ask_choice` re-prompts until the input matches a valid key. Do not regress the review loop back to bare `input()` — the editable buffer is load-bearing.

**Manual-add side door (`manual.py`) (Priority: P0)**
- **Reuses Step 2, does not fork it:** `canonicalize_url` → `db.find_by_canonical_url` (UX read only; the `UNIQUE` constraint is still the real guard) → best-effort capture → `db.insert_posting` → `db.update_jd_capture`.
- **Decided Apply on arrival:** the INSERT writes `decision = 'Apply'` (and `decided_at`) directly, so the row skips the Step 3 backlog and lands in the Step 5 tracker queue — supplying the URL *is* the decision.
- **Re-add promotes to Apply:** an existing row is upgraded via `db.set_decision` while **keeping `fit_feedback` and `search_agent`** (which record what really happened); the CLI reports the `previous_decision → Apply` transition.
- **Writes no `search_findings` row:** that table is per-agent *search-coverage* telemetry; a supplied posting would inflate an agent's coverage (consistent with PRD 02).
- **Company/title derivation:** interactive by default — `company_from_board` (pure: split the board slug on `-_.+`, title-case) offers a pre-filled company for correction, and the ATS-canonical title is offered; `--no-input` accepts the derived values or fails if they cannot be derived. `update_jd_capture` is called with `title=None` on this path so a user's title override is never clobbered by the ATS transcription.
- **Unsupported ATS is not a rejection (P0, `XC-5`):** the pipeline's index check applies only to postings an *agent* found; the user has already vouched for a hand-added one, so it inserts with a `NULL jd_markdown` if capture fails.
- **JSON-LD fallback — manual path only (P0, owned here):** capture order is **supported ATS fetcher → schema.org JSON-LD (`ats/jsonld.py`) → `NULL`**, never the reverse (Greenhouse and Lever publish no JSON-LD, so their platform fetcher is the only source). The **search path deliberately does not use this fallback** (PRD 01) — a successful JSON-LD capture is never evidence a posting is live.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa review` (local, interactive; run directly or `!`-prefixed — never as a slash command, which would reintroduce per-posting token cost). Empty backlog prints "No postings awaiting review. 🎉" and exits.
- `jsa add <URL> [--company] [--title] [--date-posted] [--no-input]` (local).

**Core Experience (review)**
1. Session opens the oldest undecided posting in Chrome and shows company/title/location/url.
2. User presses `a`/`s` (or `b`/`q`).
3. User optionally types a note, or `:a`/`:s` to flip, or `:b` to redo.
4. Decision commits; loop advances. After the last posting, one final amend pass is offered.

**Edge Cases**
- **No TTY / no `prompt_toolkit`:** degrades to `input()` (structured validation like choice-matching still enforced).
- **Chrome not installed:** the `open` call fails silently (`check=False`); review continues.
- **Ctrl-C/Ctrl-D:** caught; committed decisions persist.
- **`jsa add` on an existing row:** reports the existing id; promotes to Apply if not already, else "already Apply; no change."
- **`jsa add` capture failure / unsupported ATS:** inserts with `NULL jd_markdown`; the CLI states the Step 4 packet will have no `job_posting.md`.
- **`jsa add` aborted at a prompt before insert:** nothing is written (the insert happens after both prompts).

-----
#### Technical Considerations
- **Local-only (`XC-1`):** review needs a terminal and Chrome; manual-add needs the shared DB and outbound HTTP for capture.
- **Immediate commit (`XC-8`):** every decision persists on write.
- **Browser is hard-coded to Google Chrome on macOS** — a one-line change for another browser/OS (noted as an accepted constraint, not a portability requirement).

-----
#### Integration Points
- **Google Chrome** via the macOS `open` command (review).
- **The four ATS fetchers + `ats/jsonld.py`** (manual capture) — fetchers owned by PRD 01; unauthenticated HTTP via `httpx`.
- **Turso** (PRD 02) for all reads/writes.
- **`prompt_toolkit` / `readline` / `$EDITOR`** — line-editing (soft dependencies).

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06):
- **Google Chrome** installed locally (review).
- A terminal TTY for the rich editing experience (optional `$EDITOR` for long notes).
- `TURSO_DATABASE_URL` (+ token) — the shared DB.

-----
#### Outstanding Questions
- *(none surfaced during verification — code and docs align on this subsystem.)*
