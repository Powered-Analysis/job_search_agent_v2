# Fit Review & Decisioning
#### tl;dr

The human-in-the-loop stage where captured postings become decisions. `jsa review` is a deterministic, **no-LLM** terminal loop that opens each undecided posting in Chrome and records an Apply/Skip decision, the role family an Apply's packet starts from (PRD 04), and free-text fit feedback — the ground truth the downstream refinement loop depends on. A second, faster path, `jsa add <URL>`, lets the user hand-add a posting they already want: supplying the URL *is* the Apply decision, so it skips review entirely. A third path, the **email side door**, runs that same manual add in the cloud for a posting the user emails to their jobs mailbox, from a phone and with their computer off (`XC-1`). This spec owns PRD Step 3, the manual-add side door, and the email side door.

------
#### Goals

##### Business Goals
- **Zero-friction feedback capture:** the fit-feedback field is load-bearing ground truth (PRD 05), so editing it must be frictionless — arrow keys, word delete, and a *pre-filled editable buffer* on amend. Friction here is a design defect.
- **No token cost per posting:** review is deterministic and uses no model, so working a large backlog costs nothing beyond the human's time.
- **Every decision persists immediately:** each Apply/Skip commits on write (`XC-8`), so an interrupted session loses nothing.
- **A fast lane for known-good roles:** `jsa add` gets a role the user already found straight into the Apply queue without a review turn.
- **Apply from anywhere:** emailing a job link, or a link and its description, to the jobs mailbox gets the posting captured and decided Apply within about an hour, whether or not the user's computer is on.

##### User Goals
- As the job seeker, I want to see each undecided role in my browser and record Apply/Skip with one keystroke, so that I can clear a backlog quickly.
- As the job seeker, I want to pick each Apply's role family from a list of the ones I keep, so that its packet starts from the right base resume without my having to remember what I named them.
- As the job seeker, I want to attach a note explaining *why*, and to amend that note or flip the decision later, in the same session or afterward with `jsa revise`, so that my ground truth is accurate.
- As the job seeker, I want to drop in a URL I found myself and have it treated as Apply immediately, so that I don't review a role I've already chosen.
- As the job seeker, I want to email a role I found on my phone and trust it to land, so that finding a job doesn't wait on being at my computer.

##### Non-Goals
- **Storage of the decision/feedback** — owned by PRD 02 (the decision writes and the review backlog).
- **ATS resolution and full-JD capture mechanics** — owned by PRD 01 (resolution, the ATS fetchers, the schema.org `JobPosting` fallback this path uses, and the aggregator list) (`XC-5`).
- **What happens to an Apply row afterward** (packets, resume redlines, tracker) — PRD 04.
- **How feedback is consumed** for prompt refinement — PRD 05.
- **Any LLM in the review loop** — deliberately excluded to keep per-posting cost at zero.

-----
#### User Stories

**Job seeker**
- As the job seeker, I want the backlog presented oldest-first, so that stale postings are triaged before they close.
- As the job seeker, I want to step back to a previous posting and amend my call, so that a too-quick decision is recoverable.
- As the job seeker, I want to reopen any decided posting, or the one I decided last, to amend it after the session has ended, so that a second thought needn't wait for a final prompt.
- As the job seeker, I want `jsa add` to derive the company and title for me (and let me correct them) rather than making me type them, so that adding a role is quick.
- As the job seeker, I want re-adding a URL I already reviewed to promote it to Apply while keeping the note I wrote, so that nothing is lost.
- As the job seeker, I want to mark a role I'm adding as outside what my search looks for, so that applying to it for a one-off reason doesn't teach the search to find more like it.
- As the job seeker, I want to mark an Apply as fitting none of my role families, so that a role worth a new base resume waits for one instead of getting the wrong one.
- As the job seeker, I want the address I email a job to to name its role family, so that a phone contact per role family is my picker away from my computer.
- As the job seeker, I want postings that closed while they waited to be dropped before I see them, so that I never spend review time on a dead link.
- As the job seeker, I want review to open by telling me if the scheduled search failed, died, or never ran, so that I find out where I already look.
- As the job seeker, I want each job I email to be labeled with what happened to it, so that I can see on my phone which ones still need me.

**Developer**
- As the developer, I want the review loop to fall back to plain line input when there is no TTY, so that it does not crash in a non-interactive environment — while keeping the rich line editing when a terminal is present.
- As the developer, I want feedback-entry parsing, company-from-board derivation, email parsing, the inbox sender check, and role-family tag matching to be pure, so that they are testable in isolation, without a terminal or a mailbox (`XC-9`).

-----
#### Functional Requirements

**Review loop (Priority: P0)**
- **Search health line (P1):** before the backlog, review prints what the unattended search did, read from `search_runs`, `cron_runs` (PRD 02), and the schedule in `search.toml`. Failures surface where the user already looks rather than only in `fly logs`. It reports:
  - for each search family and agent pair the schedule uses, its latest search: date, outcome, postings inserted, and cost;
  - every failed search in the last 7 days, with its one-line error;
  - every warning those runs logged (a platform returning no dates, `reachable_no_date` and `malformed` counts);
  - every search still without an outcome 90 minutes after it started, reported as dead (the runner ceiling is an hour, PRD 01);
  - every scheduled day in the last 7 days, before today, with no `cron_runs` row, reported as missed.

  With nothing to flag, it is one line: the latest run per search family and agent. It makes no model call. A missing or invalid `search.toml` is reported in the line rather than stopping review.
- **Liveness re-check (P0, `XC-5`):** the backlog is captured, then re-checked with PRD 01's re-check before the first posting is shown. A posting found closed is marked closed (PRD 02) and leaves the backlog with no decision; the session opens by saying how many closed. A posting the re-check couldn't reach (`unverifiable`) is shown as usual, marked as not re-checked.
- **Backlog:** the review backlog (PRD 02) is undecided, unclosed rows, oldest first; it is captured once at session start so step-back navigation is stable.
- **Open in browser:** each posting opens in Google Chrome through macOS `open`. A failure to open (e.g. Chrome not installed) is ignored rather than aborting the loop, since the URL is also printed.
- **Decision prompt:** single-letter choices — **`a` = Apply, `s` = Skip, `b` = back, `q` = quit**; a bare Enter (keep the existing decision) is offered only when the posting already has a recorded decision. No pre-filled buffer at the decision prompt (a single-letter pre-fill would double the keystroke).
- **Stored vs displayed:** the keystroke `a`/`s` maps to the stored/displayed decision strings **`Apply`/`Skip`**.
- **Role family (P0):** an Apply is asked which role family (PRD 04) its packet starts from, right after the decision prompt and before the feedback prompt.
  - **A list, never a typed name:** the prompt lists every role family in the profile, numbered from 1 in name order, plus **`n` = none fits** and **`b` = back** (to the decision prompt). The user picks by key. Enter keeps the posting's current role family, and is offered only when it has one.
  - **One role family, no prompt:** with exactly one in the profile, it is chosen without asking.
  - **None fits:** `n` stores the Apply with no role family. Its packet waits until the user writes a base resume for a new role family and assigns it with `jsa revise` (PRD 04).
  - **A Skip has none:** deciding Skip, or flipping to Skip with `:s`, clears the role family. Flipping to Apply with `:a` at the feedback prompt asks the role family before anything is written.
  - *Rationale:* the user is reading the posting when they decide, so the choice is made with the most information and costs one key. A numbered list means nothing depends on remembering what the role families are called.
- **Feedback + amend (P0):** after a decision, prompt `"Feedback (Enter to skip, :a/:s to change the decision): "`. The buffer is **pre-filled with any prior feedback** so amending edits the existing note rather than retyping it. Inline commands (case-insensitive): `:a`/`:apply` and `:s`/`:skip` flip the decision and keep the remaining text as feedback; `:b`/`:back` discards and returns to the decision prompt for the same posting.
- **Revisability (P0):** within a session a posting can be revisited and re-decided (shown as " (amending)"). The session ends when the last posting in the backlog is decided. Each write refreshes `decided_at` (PRD 02), so amended rows re-enter refinement scope.
- **`jsa revise` (P0):** reopens one decided posting for amendment, outside a review session. Exactly one of `--id N` (the posting's `id`, PRD 02) or `--last` (the decided posting with the newest `decided_at`, ties to the highest `id`) is required. It shows and opens the posting like review does, then runs the same decision, role-family, and feedback prompts, with the existing decision and role family offered as the Enter defaults and the existing note pre-filled, and writes through the same decision write. An unknown `id`, a posting with no decision (the message points to `jsa review`), or `--last` with no decided postings exits non-zero with a one-line message and writes nothing. Quitting leaves the posting as it was.
- **Persistence:** a posting's decision, role family, and feedback are written together, and committed immediately, when its last prompt is submitted. Ctrl-C/Ctrl-D quits cleanly: every committed decision survives, and a posting whose prompts were interrupted keeps whatever it had before (undecided, or its earlier decision).
- **No LLM:** the loop makes no model calls.

**Prompt infrastructure (Priority: P1)**
- Line editing with arrow keys, word delete, the pre-filled editable buffer the amend flow depends on, and `Ctrl-X Ctrl-E` → `$EDITOR` for long notes (`prompt_toolkit` is approved for this). It **degrades to plain line input (with `readline`)** when stdin/stdout is not a TTY. A choice prompt re-prompts until the input matches a valid key. Do not regress the review loop back to bare `input()` — the editable buffer is load-bearing.

**Manual-add side door (Priority: P0)**
- **Reuses Step 2, does not fork it:** canonicalize → look up by canonical URL (UX read only; the `UNIQUE` constraint is still the real guard) → best-effort capture → idempotent insert → store the capture.
- **Aggregator URLs are refused (P0, `XC-3`):** a URL on PRD 01's aggregator list (LinkedIn, Indeed, and the like) is refused before anything is written, with a request for the employer's own posting URL. Cross-source dedup depends on every path storing the employer's URL, so an aggregator copy would become a duplicate row as soon as a search found the same req.
- **Decided Apply on arrival:** the INSERT writes `decision = 'Apply'` (and `decided_at` and the role family) directly, so the row skips the Step 3 backlog and lands in the Step 5 tracker queue — supplying the URL *is* the decision.
- **Re-add promotes to Apply:** an existing row's decision is changed to Apply while **keeping `fit_feedback` and `search_agent`** (which record what really happened); the CLI reports the `previous_decision → Apply` transition.
- **Refinement setting (P1):** every `jsa add` says whether the posting is ground truth for search refinement (`in_refinement`, PRD 02; how refine uses it, PRD 05). Interactive by default: after the company and title prompts, or straight after the lookup for an existing row, it asks `Use this posting to refine the search? [Y/n]`, with Enter taking the default. A new row's default is yes; an existing row's is its current setting. `--exclude-from-refine` answers no without asking. `--no-input` without it answers yes for a new row and leaves an existing row's setting as it is. A new row stores the answer in its INSERT; an existing row's answer goes through the refinement-setting write (PRD 02), so `jsa add` on a posting already added is how the user changes the setting later. The CLI's closing line says when the posting is excluded.
  - *Rationale:* the user sometimes applies to a role for reasons the search shouldn't learn from (a one-off referral, a contract to bridge a gap), and refine treats every Apply as ground truth. A hand-found role in the search's scope is the opposite case: the search missed it, the strongest positive signal refine gets, so the default is yes. A note in `fit_feedback` asking the refiner to ignore the posting would be an instruction the model may not follow, and its JD would still be sent; the setting keeps it out by construction.
- **Role family (P0):** every `jsa add` gives the posting a role family through review's role-family prompt (above), asked after the company and title prompts, or straight after the lookup for an existing row, and before the refinement question. An existing Apply row's current role family is the Enter default, and a changed answer goes through the role-family write (PRD 02). `--no-input` with exactly one role family chooses it. With more than one, a new or promoted row gets none, and an existing row's role family stays as it is. The CLI's closing line says when the posting has no role family, and that its packet waits for `jsa revise --id N`.
- **Writes no `search_findings` row:** that table is per-agent *search-coverage* telemetry; a supplied posting would inflate an agent's coverage (consistent with PRD 02).
- **Company/title derivation:** interactive by default — a company derived from the board slug (pure: split on `-_.+`, title-case) or, off the four, from the page's `JobPosting` `hiringOrganization` name is offered pre-filled for correction, and the ATS-canonical (or `JobPosting`) title is offered; `--no-input` accepts the derived values or fails if they cannot be derived. The capture stored on this path never overwrites the title the user confirmed, and refetch leaves a `manual` row's title alone too (PRD 04), so a user's title is never clobbered by the ATS's.
- **Unsupported ATS is not a rejection (P0, `XC-5`):** the pipeline's index check applies only to postings an *agent* found; the user has already vouched for a hand-added one, so it inserts with a `NULL jd_markdown` if capture fails.
- **Capture order (P0, `XC-5`):** PRD 01's **supported ATS fetcher → schema.org `JobPosting` data → `NULL`**, never the reverse. A successful capture is never evidence a posting is live; this path needs none, because the user has vouched for the posting.

**Email side door (Priority: P0)** — `jsa inbox`, the inbox machine's entrypoint (PRD 06), runs manual add for each posting the user emails to the **jobs mailbox**, a Gmail account used for nothing else.
- **The queue is the mailbox's Inbox:** each wake lists the messages in the Inbox, oldest first, and processes each one once. A processed message is archived with exactly one outcome label (below). Moving a message back to the Inbox re-queues it. Nothing else records the queue: the label is the record, and the user sees it on their phone.
- **Who may send (P0):** a message is processed only when its From address is one of `[inbox] senders` in `profile/config.toml` and the `Authentication-Results` header Gmail adds on receipt (the topmost one, from `mx.google.com`) shows DMARC passed for that address's domain. Any such header already in the message is ignored, because a sender can write one. Any other message is archived as `jsa/ignored`, its body unread, and nothing is written. *Rationale:* the mailbox address is not a secret, and an accepted message spends model calls and writes the user's tracker.
- **One posting per message:** the posting URL is the first `http(s)` URL in the message's text: its plain-text part, or, without one, its HTML part converted to text. The rest of that text, with the URL removed and the whitespace trimmed, is the **supplied description** only when it is at least 500 characters long; shorter text (a signature, "Sent from my iPhone") is ignored. *500 is a first cut:* real descriptions run to thousands of characters and signatures to tens. The owner revises it if a real message lands on the wrong side.
- **Then it is manual add, unchanged:** the same canonicalize → aggregator refusal → lookup → capture → insert-or-promote as `jsa add --no-input`, through one shared implementation. Company and title are the derived ones, because no prompt exists to correct them, a new row's refinement setting is yes, and the role family is the one the address names (below). To exclude an emailed posting, the user runs `jsa add <URL> --exclude-from-refine` locally before the next `jsa refine`.
- **The address names the role family (P0):** Gmail delivers mail sent to the jobs mailbox's address with a `+tag` added (`jobs+data-science@…`) to the same mailbox. A role family's **tag** is its name in lowercase, with each run of characters other than letters and digits replaced by one `-`, and none at either end (`Customer Enablement` → `customer-enablement`; pure, `XC-9`).
  - **Which address:** the first To or Cc address whose `+tag` matches a role family's tag.
  - **A matching tag is the answer** the role-family prompt would get: it sets the role family of a new row, a promoted row, or an existing Apply row that has none. Email never changes a role family an existing row already has.
  - **No matching tag:** with exactly one role family, that one; with more, none (see "Stops at add" below).
  - *Rationale:* a phone has no picker, but its contacts are one. The user saves one contact per role family's address and picks from the list, never typing or remembering a name.
- **Supplied description (P0, `XC-5`):** used only when capture yields no description, as the row's `jd_markdown`. A captured description always wins.
- **Stops at add without a role family or a description:** the posting is added (or promoted) as Apply and the run stops there. With no role family it is labeled `jsa/needs-family`, and the user's next local `jsa generate` flags it for `jsa revise` (PRD 04). With a role family but no description from capture or the email, it is labeled `jsa/needs-jd`, and `jsa generate` flags it for a hand-filled `job_posting.md`.
- **With a role family and a description, the run continues** to that posting's packet and tracker row (PRD 04 "Packets built on the inbox machine").
- **Outcome labels:** exactly one per processed message.

  | Label | Meaning |
  |---|---|
  | `jsa/tracked` | The packet is in Drive and the tracker row is appended; or the posting was already tracked, so nothing was rebuilt. |
  | `jsa/needs-family` | The posting is Apply, but the address named no role family. Finish it locally: `jsa revise` gives it one. |
  | `jsa/needs-jd` | The posting is Apply, but no description was captured or emailed. Finish it locally. |
  | `jsa/needs-fields` | The company or title couldn't be derived; nothing was written. Add it locally with `jsa add`. |
  | `jsa/refused` | The message has no URL, or an aggregator URL; nothing was written. |
  | `jsa/ignored` | Not from an allowed, authenticated sender; nothing was read past the headers or written. |
  | `jsa/failed` | Any other error. The posting stays wherever the run stopped: an untracked Apply row is finished by the next local `jsa generate`, or by re-queuing the message. |

- **Crash-safe:** labeling and archiving are a message's last step, so a wake that dies mid-message processes it again on the next wake. Manual add's idempotency and the tracker guard (`XC-3`, `XC-10`) make the repeat safe.
- **One message's failure never stops the rest**, and no log line holds a message body or a credential.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa review` (local, interactive; run directly or `!`-prefixed — never as a slash command, which would reintroduce per-posting token cost). Empty backlog prints "No postings awaiting review. 🎉" and exits.
- `jsa add <URL> [--company] [--title] [--date-posted] [--exclude-from-refine] [--no-input]` (local).
- `jsa revise (--id N | --last)` (local, interactive): amend a decided posting.
- The email side door: the user emails the jobs mailbox a posting's link, or its link and its pasted description, one posting per message; `jsa inbox` on the inbox machine picks it up within the hour (PRD 06).

**Core Experience (review)**
1. Session prints the search health line, re-checks the backlog, says how many postings closed, then opens the oldest undecided posting in Chrome and shows company/title/location/url.
2. User presses `a`/`s` (or `b`/`q`). For an Apply, the user picks its role family from the numbered list.
3. User optionally types a note, or `:a`/`:s` to flip, or `:b` to redo.
4. Decision commits; loop advances. The session ends after the last posting.

**Edge Cases**
- **No TTY:** degrades to plain line input (structured validation like choice-matching still enforced).
- **Chrome not installed:** the open fails silently; the printed URL remains, and review continues.
- **Offline or an ATS outage at session start:** the affected postings re-check as `unverifiable` and are shown as usual, marked as not re-checked.
- **Every backlog posting closed:** the session reports how many closed, then the empty-backlog message.
- **A scheduled search failed, died, or never ran:** the health line names it; review proceeds.
- **Ctrl-C/Ctrl-D:** caught; committed decisions persist.
- **`jsa add` on an existing row:** reports the existing id; promotes to Apply if not already; changes the role family or the refinement setting if the answer differs; with none of these changes, "already Apply; no change."
- **An Apply that fits no role family:** stored with none; `jsa generate` names it until `jsa revise` gives it one (PRD 04).
- **A role family changed after the posting's packet was built** (with `jsa revise` or `jsa add`): the new role family is stored, and the packet and tracker row already made stay as they are. Nothing is rebuilt or moved.
- **An emailed posting sent to the plain address, or with a tag that matches no role family,** when the profile has more than one: `jsa/needs-family`.
- **`jsa add` capture failure / unsupported ATS:** inserts with `NULL jd_markdown`; the CLI states the Step 4 packet will have no `job_posting.md`.
- **`jsa add` aborted at a prompt before insert:** nothing is written (the insert happens after every prompt). On an existing row, quitting at the refinement prompt writes nothing either.
- **`jsa add` with an aggregator URL:** refused before anything is written; the CLI asks for the employer's own posting URL.
- **A link shared from LinkedIn or another aggregator by email:** `jsa/refused`; the user sends the employer's own link instead.
- **An email with several links (a forwarded recruiter email):** the first is the posting; a wrong pick shows as the wrong company or title in the tracker, and the user re-sends the link alone.
- **The same posting emailed twice:** the second finds it already Apply, and already tracked if the first finished, and is labeled to match; nothing is built twice.
- **An allowed sender's message that fails DMARC:** `jsa/ignored`, as from anyone else.
- **The inbox machine can't read the mailbox** (an expired or revoked credential): every message stays unlabeled in the Inbox. A message with no label after an hour is the user's signal; `fly logs` names the failure.

-----
#### Technical Considerations
- **Where each runs (`XC-1`):** review is local only, and needs a terminal, Chrome, and outbound HTTP for the re-check. Manual add runs locally (`jsa add`) and on the inbox machine (the email side door); both need the shared DB and outbound HTTP for capture, and the inbox also reads Gmail through `gws` with the jobs mailbox's credential (PRD 06).
- **Hourly, not instant:** the inbox machine wakes hourly (PRD 06), so an emailed posting lands within about an hour. The user sends and moves on; nothing waits on a reply.
- **Immediate commit (`XC-8`):** every decision persists on write.
- **Browser is hard-coded to Google Chrome on macOS** — a one-line change for another browser/OS (noted as an accepted constraint, not a portability requirement).

-----
#### Integration Points
- **Google Chrome** via the macOS `open` command (review).
- **The four ATS fetchers + the `JobPosting` extractor** (manual capture) — both owned by PRD 01; unauthenticated HTTP via `httpx`.
- **Turso** (PRD 02) for all reads/writes.
- **Gmail** via the `gws` CLI, with the jobs mailbox's credential (the email side door).
- **`prompt_toolkit` / `readline` / `$EDITOR`** — line-editing (soft dependencies).

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06):
- **Google Chrome** installed locally (review).
- A terminal TTY for the rich editing experience (optional `$EDITOR` for long notes).
- `TURSO_DATABASE_URL` (+ token) — the shared DB.
- For the email side door: the jobs mailbox, its `gws` credential on the inbox app, and `[inbox] senders` in `profile/config.toml`.

-----
#### Outstanding Questions
- None open.
