# Application Outputs
#### tl;dr

What an Apply decision turns into: a per-job **application packet** on disk (`{packets_dir}/{company} - {title}`) holding the job description, a **working copy of the user's single base resume**, and a **resume checklist** — an agent's assessment of the packet's resume against the posting, read as a hiring manager would read it, written to guide the user's own revision — plus a row appended to the **Google Sheet tracker**. The app never writes resume content: revision is the user's. It also owns **refetch**, which reconciles stored postings against their ATS record when a req is edited under a stable URL. This spec covers PRD Steps 4–5 plus reconciliation.

------
#### Goals

##### Business Goals
- **One command from decision to revision-ready:** `jsa generate` takes an Apply row to a finished packet (JD, resume copy, checklist) and a tracker row, on a bounded worker pool.
- **A human revises; the agent advises.** An agent tailoring a resume from a job description alone lacks the context to do it without visibly overfitting to the posting. So the agent's output is a checklist for the user's manual revision, and the app writes no resume content.
- **One base resume:** every packet starts from the same file, `profile/resume.docx`. No step chooses between resumes.
- **The checklist sees what a hiring manager sees:** the posting and the resume, nothing else. No profile context reaches it, so a strength the resume does not state on its face reads as absent, exactly as it would to the reader it is written for.
- **The tracker is a faithful, non-destructive projection:** appends never break the Status dropdown or shift its data-validation ranges (`XC-4`).
- **Idempotent outputs:** `added_to_tracker` is the single completion guard, so re-runs never double-build or double-append (`XC-10`).
- **Drift never silently strands a stale packet, and never destroys revision work:** when an employer edits a req, refetch reconciles the DB and refreshes the affected packet in place.

##### User Goals
- As the job seeker, I want a per-job folder holding the JD, a copy of my resume ready to edit, and a checklist of what to strengthen for this posting, so that my revision starts from a clear assessment instead of a blank read.
- As the job seeker, I want a single tracker where the agent fills the posting columns and I own the application-state columns.

##### Non-Goals
- **Generating or editing resume content.** The checklist assesses the resume; the user revises it.
- **More than one base resume, or any selection among resumes.** One base resume serves every posting.
- **Posting data storage and queue queries** — PRD 02 (the packet, tracker, and refetch queues, marking tracked, and JD capture).
- **ATS fetcher shapes** — PRD 01 (refetch reuses them).
- **Application state (Date Applied, Status)** — the user's columns, never agent-written (`XC-4`).

-----
#### User Stories

**Job seeker**
- As the job seeker, I want the checklist to tell me which of my strengths this posting rewards and where my resume falls short of it, so that I revise where it matters.
- As the job seeker, I want my edits to the resume copy in a packet never to be overwritten, so that my revision work is safe.
- As the job seeker, I want a refreshed checklist to assess my revised resume rather than the base, so that it tells me what still falls short, not what I already fixed.
- As the job seeker, I want a hand-filled `job_posting.md` to be honored when the ATS gave no JD, so that I can still get a checklist for an unsupported platform.
- As the job seeker, I want a corrected title on an unapplied role to update its tracker cell, so that the Sheet stays accurate.

**Operator / Developer**
- As the operator, I want a failed tracker append to leave the row in the backlog (never optimistically flagged), so that no job silently drops out of the tracker.
- As the developer, I want resume rendering, file naming, and tracker row building to be pure, so they are testable while the checklist step stays agentic (`XC-9`).
- As the developer, I want `jsa generate` to re-enter a bare packet directory (left by `jsa packet`, an interrupted run, or a refetch rename) rather than abort, because the completion guard is `added_to_tracker`, not directory-exists (`XC-10`).

-----
#### Functional Requirements

**Base resume (Priority: P0)**
- **One file:** `profile/resume.docx` (`XC-11`). A missing or empty file raises before any row is processed, pointing to `profile.example/`.
- **Rendered to text** (pure over the loaded document; `python-docx` is approved for reading it): paragraphs in order, bold preserved as `**…**`, so the checklist agent sees a resume's structure and emphasis.

**Packet directory (Step 4's head) (Priority: P0)**
- Creates `{packets_dir}/{normalized_company} - {title_slug}` (`packets_dir` from `profile/config.toml`, default `~/Documents/Job Applications`; name from PRD 01's naming fields), writes `job_posting.md` from `jd_markdown`, and copies the base resume in as `{resume_file_stem}.docx`, the user's working copy. File names are `{candidate_name}_Resume_{title_slug}_{normalized_company}` with spaces removed (pure; `candidate_name` from `profile/config.toml`); the candidate prefix is omitted when the key is unset.
- **Name collisions:** two postings can share a normalized company and title slug (two reqs for the same role). The lowest-id posting gets the plain directory name, and each other's directory name gets ` ({id})` appended, so a posting never re-enters another posting's packet. The rule reads only the DB, so every command and refresh derives the same name.
- **Never overwrites the resume copy:** an existing file at that path is left alone, since it may hold the user's edits.
- Standalone `jsa packet` **skips a posting whose directory already exists** (never clobbered) and writes no checklist; a `NULL`-JD row still gets its directory and resume copy (no `job_posting.md`). Queue: the packet queue (PRD 02; `--id` waives the tracker condition, never Apply).

**Resume checklist (Step 4) (Priority: P0)**
- **Queue:** the packet queue (PRD 02). Unlike `jsa packet`, generate **ensures** (re-enters) a bare directory (`XC-10`), completing the packet head before writing the checklist.
- **Assesses the packet's resume copy, never the base:** the checklist always reads `{resume_file_stem}.docx` as it currently stands in the packet. On a first run that copy is identical to the base; on a refresh (`--id`, refetch) it is the user's revision in progress.
- **Agent:** the Claude Agent SDK, headless, **`model` and `effort` from `[agents.checklist]` in `profile/config.toml`** (`XC-14`; example default `claude-fable-5-1` at `medium`), no tools at all, a single turn, run through the shared agent loop (`XC-12`), so an API error result raises with the real HTTP status. Its final text is written to the packet as `resume_checklist.md`; an empty result raises.
- **What the checklist is:** an assessment of the resume against this posting, from the hiring manager's seat (which strengths to lead with, which requirements it leaves unmet or under-evidenced, and what to revise), for the user to work through by hand. Its content, structure, and standards live in the prompt template; this spec owns its inputs, its output, and its boundary: it advises, and nothing in the app applies it.
- **Prompt (`XC-13`):** the app's checklist template, assembled with the slots below. It has no profile slots: the template is candidate-agnostic (`XC-11`), and the posting and the resume are its only inputs, by design.

  | Slot | Source | Required | Precedence |
  |---|---|---|---|
  | `{{JOB_TITLE}}`, `{{COMPANY}}`, `{{JOB_DESCRIPTION}}` | the posting row (or a hand-filled `job_posting.md`) | — | Input. |
  | `{{RESUME}}` | the packet's resume copy, rendered to text | yes | The document under assessment; the only evidence about the candidate. |

- **Never review blind:** a `NULL`-JD row gets its packet head but no checklist, is flagged, and stays in the queue untracked. An existing hand-filled `job_posting.md` is used as the JD and never overwritten from a `NULL` `jd_markdown`.
- **Re-entry:** the queue path writes the checklist only when the packet lacks one, so a run interrupted after the checklist resumes at the tracker append. `--id` and a refetch refresh always rewrite it, against the resume copy's current contents.
- **Concurrency & seam:** a bounded worker pool (`JSA_GENERATE_WORKERS`, default 3) with tracker appends serialized; each finished row is appended to the tracker right away, exactly as `jsa track --id` would (the Step 4→5 seam). A failed generate or track is flagged and exits non-zero; never a rollback.

**Tracker write (Step 5) (Priority: P0)**
- **Row shape A:H** (pure): **A** ID (`postings.id`, the join key), **B** Company (`normalized_company`), **C** Title, **D** URL, **E** Date Posted, **F** Date Added (today, in the profile's `timezone`), **G** Date Applied (blank — user-owned), **H** Status (blank — user-owned dropdown).
- **Append:** shells out to the local `gws` CLI (`JSA_GWS_BIN` override) with `valueInputOption = USER_ENTERED` and **`insertDataOption = OVERWRITE`** (never `INSERT_ROWS`). *Rationale:* `INSERT_ROWS` lands the row outside the Status column's data-validation and conditional-formatting ranges (losing the dropdown) and shifts those ranges down each time; `OVERWRITE` writes into pre-formatted blank rows. Measured on the live sheet; do not "restore" `INSERT_ROWS`.
- **Posting text is never evaluated (P0):** Company, Title, and URL come from employer pages, and `USER_ENTERED` would run any of them that looks like a formula. A cell value beginning with `=`, `+`, `-`, or `@` is written as literal text.
- **Idempotency & safety (P0):** eligibility is the tracker queue (`Apply AND added_to_tracker = 0`, PRD 02); the append **raises on any ambiguity** (non-zero `gws` exit, unparseable output, a response reporting no updated row) because optimistically flagging on exit 0 would drop a job from the tracker permanently. Rows are appended one at a time so one failure cannot strand the rest; a row is marked tracked only after a confirmed append.
- **Projection reads and writes:** rewriting a row's Title cell (`C{row}`, used by refetch); reading the tracker index (`postings.id → {row number, Date Applied}`, skipping non-integer ID cells); authority never flows Sheet→DB (`XC-4`).

**Reconciliation (Priority: P1)**
- **Scope (default):** `Apply` rows that are **absent from the Sheet OR have a blank Date Applied** — the ones where drift could still change the user's next action. The Sheet index read is *fatal* in the default scope (guessing defeats it) and best-effort under `--id`/`--all` (where it only enables Title propagation). `--all` widens to every row; `--id` targets one unconditionally.
- **Re-apply the insert rule:** re-read the ATS record (off the four, the posting page's `JobPosting` data, PRD 01); the ATS-canonical title wins and `title_slug` is re-derived (PRD 02's JD capture), except on a `manual` row, whose title is the one the user confirmed at add (PRD 03).
- **Failed fetch leaves the row *completely* untouched** (`XC-6`) — never trade a good capture for a blip; for a pulled posting the stored JD is the only surviving record.
- **Title propagation:** a corrected title on a tracked-but-unapplied row updates the Sheet Title cell; a failed Sheet write degrades to a flagged hand-fix, never blocks the reconciliation.
- **Packet refresh on drift, in place:** a title/description change on a row with an existing packet directory renames the directory and the resume copy to their new names when the title changed, then regenerates the row exactly as `jsa generate --id` would, which rewrites `job_posting.md` and regenerates the checklist against the (possibly revised) resume copy. Refetch never deletes a packet directory or any file in it, and renames only, never rewrites, the resume copy, because it may hold the user's revision work. If a distinct directory or resume file already exists at a new name, refetch renames nothing and flags the row. A location-only change touches nothing; refetch never creates a packet where none existed. A failed generate is flagged for a manual re-run, never rolled back.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa packet [--id] [--dry-run]`, `jsa generate [--id] [--dry-run]`, `jsa track [--id] [--dry-run]`, `jsa refetch [--id] [--all] [--dry-run]` (all local). First-time setup (the base resume, `gws` OAuth, the Sheet) is owned by PRD 06.
- Dry-runs preview the queue/paths/rows, make no model call, and write nothing.

**Core Experience (`jsa generate`)**
1. For each Apply+untracked row: ensure the packet directory, `job_posting.md`, and the resume copy.
2. Assemble the checklist prompt with the JD and the text of the packet's resume copy.
3. Run the single-turn agent; write `resume_checklist.md`.
4. Append the tracker row via `jsa track --id`.
5. The user works through the checklist, revising the resume copy by hand.

**Edge Cases**
- **`NULL` JD, no hand-filled `job_posting.md`:** packet head only, no checklist, flagged; the row stays queued (never reviewed blind).
- **Missing or empty `profile/resume.docx` or `tracker_spreadsheet_id`:** raises before any row is processed, pointing to `profile.example/`.
- **Agent API error (429/500/529 outliving the CLI's retries) or an empty result:** raised by the shared loop; the row is flagged `failed` and stays in the queue.
- **Resume copy already present:** left untouched, whichever command or refresh reaches it; the checklist assesses it as it stands.
- **Resume copy deleted by the user:** the next generate copies the base in afresh before assessing.
- **Tracker append ambiguous/failed:** row stays in the backlog; `jsa track` is the recovery path.
- **Refetch drift on a tracked-but-unapplied row:** DB updated, Title cell refreshed, packet renamed and refreshed in place; a Sheet-write failure becomes a flagged hand-fix.
- **Refetch rename target already exists:** nothing renamed; flagged.
- **Two postings with the same company and title:** the later one's packet directory carries ` ({id})`; neither touches the other's files.
- **A title or company beginning with `=`, `+`, `-`, or `@`:** written to the Sheet as literal text, never as a formula.
- **Standalone `jsa packet` on an existing directory:** skipped, never clobbered.

-----
#### Technical Considerations
- **Local-only (`XC-1`):** needs the local disk, the local profile, and the `gws` OAuth token; never runs in the cloud.
- **Deterministic core, agentic shell (`XC-9`):** resume rendering, resume file naming, and tracker row building are pure; the checklist is one agentic call whose output is advisory, so a non-reproducible checklist never makes the packet itself non-reproducible.
- **Completion guard is `added_to_tracker`, not directory-exists (`XC-10`):** `jsa generate` must re-enter a bare directory.
- **`gws` ambiguity is fatal by design:** optimistic flagging would permanently drop a job.
- **Cost:** one model call per Apply row (plus one per `--id` or refetch refresh), sized by the JD and the resume; a single turn, no iteration.

-----
#### Integration Points
- **`python-docx`** (approved) — reads the packet's resume copy for rendering.
- **Claude Agent SDK** (the profile's checklist model), tool-less; Claude auth inherited from the environment (PRD 06).
- **Google Sheets** via the local **`gws` CLI** (`JSA_GWS_BIN`), which holds the Google OAuth token locally.
- **The four ATS fetchers and the `JobPosting` fallback** (PRD 01) — refetch capture.
- **Turso** (PRD 02).

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06; all local-only profile content, `XC-11`):
- **`profile/resume.docx`** — the single base resume every packet starts from.
- **`gws` CLI + Google OAuth** (`gws auth login`) for Sheet writes; note the testing-status OAuth 7-day token expiry until the consent screen is published.
- **The tracker Google Sheet** with an `Applications` tab, an A:H header, and a Status dropdown / data-validation already set up; its id as **`tracker_spreadsheet_id`** in `profile/config.toml` (no code default).
- **`candidate_name`** (the resume file-name prefix) in `profile/config.toml`, optional.

-----
#### Outstanding Questions
- None open.
