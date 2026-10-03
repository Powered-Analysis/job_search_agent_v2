# Dynamic Development
#### tl;dr

The part of the system that **improves with use**. The **search-profile refinement loop** (`jsa refine`) turns the user's accumulated ground truth into a better search: it reads decided postings and their JDs and stages proposed edits to the user's search fragments, which the user then accepts or rejects. It is incremental (scoped by a database run-record), runs locally, and keeps a human as the only gate. This spec covers PRD's "Search Prompt Updates" learning mechanism.

------
#### Goals

##### Business Goals
- **The search gets sharper without manual analysis:** each run, the refiner mines new decisions (and the implicit patterns in their JDs) into concrete fragment edits, staged for review.
- **Incremental, never re-litigating settled ground:** each run scopes to only the decisions made since the last recorded run, so cost scales with new ground truth, not total history.
- **A human is the only gate:** the refiner can never write the live fragments — it edits scratch copies, and nothing changes until the user resolves and accepts (no sentinels, no pinning test, by choice). Nothing reaches the cloud until the user also runs `jsa deploy`.

##### User Goals
- As the job seeker, I want the search to learn from my Apply/Skip calls, so that it surfaces more of what I want and less of what I skip.
- As the job seeker, I want to review every proposed prompt change before it ships, so that the search prompt never drifts silently.

##### Non-Goals
- **The run-record table's schema** (`prompt_refinement_runs`) — PRD 02; this spec owns how the loop *uses* the cutoff.
- **How the search prompt is executed** (PRD 01).
- **Learning from the user's resume revisions.** Revision is manual (PRD 04), and nothing reads the revised resumes back.
- **Deploying accepted fragments** — `jsa deploy` (PRD 06).
- **Watermarking or ground-truth references inside the search fragments** — deliberately excluded to keep them standalone (`XC-7`).
- **The authored *prose* of the templates and fragments** (the refine and search templates, the user's fragments) — this spec owns the refinement *loop*, its slots, and its write set, not the wording of the documents it reads and edits.

-----
#### User Stories

**Job seeker**
- As the job seeker, I want each refine run to leave its proposal as my fragments with merge-conflict markers, so that I see every change at once, in context, and keep exactly the ones I want before it touches my live search.
- As the job seeker, I want re-deciding a posting to bring it back into the refiner's scope, so that a corrected call is learned from.

**Operator / Developer**
- As the operator, I want an errored run to record nothing, so that its inputs are reconsidered on the next run rather than silently lost.
- As the developer, I want to run the loop by hand (`--dry-run` to preview scope), so that I can debug without a model call or a recorded run.

-----
#### Functional Requirements

**Search-profile refinement (Priority: P1)**
- **Incremental scope:** the cutoff is the newest `cutoff` recorded in `prompt_refinement_runs` (PRD 02), and the scope is decided rows with `decided_at` after it. On the **first-ever run** (no cutoff) every decided row is in scope; afterwards `decided_at > cutoff` scopes it, and re-deciding a row (which refreshes `decided_at`) re-enters it.
- **Every in-scope row, every run:** a run sends the whole scope, never a subset, because any rule for choosing which decisions to show the refiner would bias what it learns. The first-ever run therefore sends every decided JD.
- **The cutoff is what the run saw, not when it finished:** a run records as its `cutoff` the newest `decided_at` among the rows in its scope. A decision made while a run is in flight is therefore considered next time rather than skipped.
- **Ground truth assembled by the caller:** the rows in scope are rendered **in full** (id, company, title, decision, `fit_feedback`, `search_agent`, url, location, `date_posted`, **full `jd_markdown`**, `decided_at`) — the implicit-pattern-mining input; the decided history is rendered as a **compact no-JD, no-feedback** one-line-per-row reference (with Apply/Skip counts) for confirming a pattern recurs.
- **Write set is structural (P0):** the refiner may change only `target_roles.md`, `filters.md`, `positive_signals.md`, `negative_signals.md`, and `hard_exclusions.md`. It never touches `candidate.md` (facts about the person are not learnable from decisions), `search.toml`, or any app template — so the output contract, the `{{SEARCH_WINDOW}}` slot, and the liveness gates are out of reach by construction, not by instruction.
- **Refiner agent:** the Claude Agent SDK over a **scratch directory** — **`model` and `effort` from `[agents.refine]`** (`XC-14`; example default `claude-opus-5-5` at `high`), at most 80 turns, working in a temporary scratch directory that `jsa refine` fills with copies of the five refinable fragments before the run and discards after rendering the proposal. **Its only tools are `Read` and `Edit`, and neither reaches outside the scratch directory; every other tool is unavailable.** The ground truth carries full JDs from employer pages, which is untrusted text on the user's own machine. No DB access. Run through the shared agent loop (`XC-12`), which raises on an error result before anything is recorded. Its **final message becomes the proposal's rationale**: a numbered list of changes, each with its evidence, the fragments it touches, and any other change it depends on (take both or neither). Anything too ambiguous to encode goes into the rationale as an open question — there is no TODO file.
- **Prompt (`XC-13`):** the app's refine template with slots `{{GROUND_TRUTH}}` and `{{HISTORY}}` (run-time, above) and `{{SEARCH_PROMPT}}` — the *fully assembled* current search prompt (its `{{SEARCH_WINDOW}}` filled with a fixed note that the window varies per run, since refine has none), so the refiner judges fragments in the context of the machinery around them while only being able to edit the fragments.
- **Fragments stay standalone (`XC-7`):** the refine template instructs the agent to translate ground truth into fragment edits (explicit directives → hard exclusions; objective, posting-verifiable criteria → filters; recurring patterns → negative signals / sharpened target language; one-off judgment → stays out; a negative signal that recurs with zero Applies → a proposed promotion, flagged prominently in the rationale) while keeping the fragments free of watermarks or ground-truth references. Oscillation across runs is an accepted cost.
- **Run recording:** the run is recorded on completion (advancing the cutoff) **whether or not the proposal is accepted** — a rejected translation still *considered* its rows. An **errored run records nothing**, so its rows are reconsidered. `--dry-run` reports scope only (no model call, no record).

**Proposal review (`jsa refine --accept | --reject`) (Priority: P1)**
- **The proposal is conflict-marked fragments:** a completed run with edits writes `profile/refine/`, holding `rationale.md` and, for each fragment the refiner changed, a copy of the live fragment in which every changed region is a git-style conflict block (`<<<<<<< current` … `=======` … `>>>>>>> proposed`; a pure insertion or deletion has an empty side). Unchanged fragments are not written. The blocks come from a line diff of live against proposed (pure); the agent never writes markers. A hidden record in the same directory holds the hashes of the live fragments the proposal was built from. `jsa refine` ends by printing the directory's path. A run with no edits writes nothing.
- **The user resolves in place:** in each file, keep the side wanted, or write something else entirely, and delete the markers. That's the whole review: every change is visible in context at once, so a partial acceptance is decided against the entire proposal, and the rationale's dependency notes keep coupled changes together.
- **`--accept`** refuses while any proposal file still contains a marker line (naming the file and line), if the resolved fragments fail the search prompt's assembly (e.g. a required fragment resolved to empty, named the same way), or if any live fragment's hash no longer matches the record (the user edited it by hand after the run — reject and re-run, so an accept never silently overwrites a hand edit). Otherwise it shows the final diff of each resolved file against its live fragment, replaces the live fragments, clears `profile/refine/`, and prints a reminder that `jsa deploy` ships the change to the cloud.
- **`--reject`** clears `profile/refine/`; the live fragments are untouched.
- **One proposal at a time:** while `profile/refine/` holds a proposal, a new `jsa refine` run refuses (accept or reject first), and `jsa deploy` warns that it is pending and will not ship.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa refine [--dry-run | --accept | --reject]` (local, run by hand). Empty scope prints a quiet "nothing new" and exits.

**Core Experience (`jsa refine`)**
1. Compute the cutoff; gather every in-scope decided row (full, with JDs) + compact history.
2. Copy the five refinable fragments into a scratch directory and assemble the current search prompt for context.
3. Run the refiner agent in the scratch directory; it edits the copies, and its final message is the rationale.
4. Write `profile/refine/`: the rationale and a conflict-marked copy of each changed fragment.
5. The user reads the rationale, resolves the markers in their editor, and runs `jsa refine --accept` (or `--reject`), then `jsa deploy` to ship an accepted change.

**Edge Cases**
- **No new ground truth:** quiet exit; no run recorded.
- **A decision made while a run is in flight:** its `decided_at` is newer than the run's recorded cutoff, so the next run considers it.
- **Agent error (HTTP/tooling):** raises before recording; the run's inputs are reconsidered next time.
- **Scope larger than the refine model's context window (accepted risk):** the prompt is rejected as an agent error and nothing is recorded. Scope only grows, so refine stays blocked until `[agents.refine]` names a model with a larger context window; a subset is never sent instead.
- **Refiner proposes no changes:** the run is still recorded (cutoff advances); nothing is written.
- **Proposal already pending:** `jsa refine` refuses before any model call; accept or reject first.
- **Markers left unresolved:** `--accept` refuses, naming the file and line.
- **A required fragment resolved to empty:** `--accept` refuses, naming the fragment, so a broken search never reaches `jsa deploy`.
- **Live fragment hand-edited after the run:** `--accept` refuses; reject and re-run.
- **A change kept only in part:** whatever the resolved file says is what ships; a dropped change is not remembered, so later evidence may propose it again (the accepted oscillation).
- **`ANTHROPIC_API_KEY` set alongside the OAuth token in local `.env`:** the CLI prefers the API key and 401s on the OAuth flow — the env must provide exactly one (PRD 06).

-----
#### Technical Considerations
- **Incrementality lives in the DB, not the artifact (`XC-7`):** the run-record cutoff is what keeps the loop incremental, so the search fragments carry no watermark and stay standalone.
- **Record on completion:** refine records whether or not the proposal is accepted (a considered row is considered), and leaves an errored run unrecorded so nothing is silently skipped.
- **Model and effort are the user's (`XC-14`):** the example default puts Opus at high effort on this judgment-heavy work; the app fixes only the loop's tools and write set.
- **Local only (`XC-1`):** the loop runs by hand on the user's machine and needs the DB and the local profile.
- **Guardrails by construction, not instruction:** the scratch directory and the fixed write set are what keep the refiner off the output contract and liveness gates, so the refine template does not have to police them in prose.

-----
#### Integration Points
- **Claude Agent SDK** — the profile's refine model; auth inherited from the environment (PRD 06).
- **Turso** (PRD 02) — scope reads + run records.
- **`profile/`** (`XC-11`) — refine stages against `profile/search/`.

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06):
- **`[agents.refine]`** in `profile/config.toml`.
- Claude auth in the environment (exactly one of OAuth token / API key).

-----
#### Outstanding Questions
- None open.
