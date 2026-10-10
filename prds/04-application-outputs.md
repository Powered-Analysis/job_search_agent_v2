# Application Outputs
#### tl;dr

What an Apply decision turns into: a per-job **application packet** on disk (`{packets_dir}/{company} - {title}`) holding the job description, a **working copy of the user's single base resume** (with a PDF beside it when the redline changed nothing), a copy of the user's cover letter when they keep one (with a PDF beside a `.docx` one), and a **resume checklist** — an agent's assessment of the packet's resume against the posting, read as a hiring manager would read it, written to guide the user's own revision — plus a row appended to the **Google Sheet tracker**. The app never revises the resume: revision is the user's. Its one hand on the resume's wording is the **ATS redline**, which writes proposed wording changes into the packet's resume copy as Word tracked changes, each traced to a verbatim quote from the posting, for the user to accept or reject one by one. In the resume's skills section, and only there, the redline may also add, remove, or reword skills, drawing on a skills list the user keeps. The app also fills three bracketed placeholders (`[COMPANY]`, `[TITLE]`, `[DATE]`) in the packet's resume and cover letter copies from the posting. A posting that arrives through the email side door (PRD 03) has its packet built on the inbox machine and delivered to the same folder through Google Drive (`XC-1`). It also owns **refetch**, which reconciles stored postings against their ATS record when a req is edited under a stable URL. This spec covers PRD Steps 4–5 plus reconciliation.

------
#### Goals

##### Business Goals
- **One command from decision to revision-ready:** `jsa generate` takes an Apply row to a finished packet (JD, resume copy, checklist, redline) and a tracker row, on a bounded worker pool.
- **A human revises; the agent advises.** An agent tailoring a resume from a job description alone lacks the context to do it without visibly overfitting to the posting. So the agent's output is a checklist for the user's manual revision, and the app never revises the resume.
- **One base resume:** every packet starts from the same file, `profile/resume.docx`. No step chooses between resumes.
- **The checklist sees what a hiring manager sees:** the posting and the resume, nothing else. No profile context reaches it, so a strength the resume does not state on its face reads as absent, exactly as it would to the reader it is written for.
- **ATS wording, traced to the posting:** an ATS matches the posting's literal terms, and a resume can name the same thing in different words. Outside the skills section, the redline closes that gap and nothing else. It is about ATS matching, never positioning or qualifications. Every change it proposes there cites the posting text that motivates it and leaves what the bullet claims unchanged. A change lands only when the user accepts it.
- **The skills section draws on the user's own list:** a skills section is a list, not a claim about one job, so the redline has more room there. It may add, remove, or reword skills, first for ATS matching and second for a consistent narrative. A skill reaches the section only from the list the user keeps or in the posting's own words.
- **The packet's resume files say whether the resume is ready:** when the redline changed nothing, the packet holds the resume as `.docx` and `.pdf`, ready to submit. When it proposed changes, the packet holds one resume file, the redlined `.docx`, because that is the copy that becomes the submitted version once reviewed.
- **The tracker is a faithful, non-destructive projection:** appends never break the Status dropdown or shift its data-validation ranges (`XC-4`).
- **Idempotent outputs:** `added_to_tracker` is the single completion guard, so re-runs never double-build or double-append (`XC-10`).
- **One home for packets:** with the inbox in use, `packets_dir` is the Drive for Desktop mirror of a Google Drive folder, so a packet built locally and one built on the inbox machine land in the same place.
- **Drift never silently strands a stale packet, and never destroys revision work:** when an employer edits a req, refetch reconciles the DB and refreshes the affected packet in place.

##### User Goals
- As the job seeker, I want a per-job folder holding the JD, a copy of my resume ready to edit, and a checklist of what to strengthen for this posting, so that my revision starts from a clear assessment instead of a blank read.
- As the job seeker, I want the resume's wording aligned with the posting's own terms where they name the same thing, so that an ATS matches it, without any change to what my resume claims.
- As the job seeker, I want a single tracker where the agent fills the posting columns and I own the application-state columns.

##### Non-Goals
- **Writing resume content on the user's behalf.** The checklist assesses the resume, the redline proposes term alignments and skills-section changes, and the user revises and decides. No step changes what a bullet claims or rewords for style, and no skill reaches the resume that is neither on the user's list nor in the posting's words. The only text the app writes into the packet's resume copy is filled placeholders and tracked changes the user can reject.
- **More than one base resume, or any selection among resumes.** One base resume serves every posting.
- **Tailoring, generating, or reading the cover letter.** It is copied into the packet as the user's file; the checklist and the redline never see it. Filling its placeholders is the only change the app makes to it.
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
- As the job seeker, I want each redline change to show the posting text behind it, beside the change in Word, so that I can judge whether it's warranted and whether it keeps my meaning.
- As the job seeker, I want a redline that has no posting text behind it, or that brings in words the posting doesn't use, to be impossible rather than merely discouraged, so that I can trust what the redline proposes is traced.
- As the job seeker, I want the skills section revised from a list of skills I actually have, so that it matches the posting without claiming anything new.
- As the job seeker, I want a packet whose resume needed no wording changes to hold a PDF ready to submit, and a packet with proposed changes to hold only the copy I must review, so that I never submit the wrong file.
- As the job seeker, I want the company, title, and date filled in wherever I marked them in my resume and cover letter, so that I don't retype them for every application.

**Operator / Developer**
- As the operator, I want a failed tracker append to leave the row in the backlog (never optimistically flagged), so that no job silently drops out of the tracker.
- As the developer, I want resume rendering, file naming, and tracker row building to be pure, so they are testable while the checklist step stays agentic (`XC-9`).
- As the developer, I want `jsa generate` to re-enter a bare packet directory (left by `jsa packet`, an interrupted run, or a refetch rename) rather than abort, because the completion guard is `added_to_tracker`, not directory-exists (`XC-10`).

-----
#### Functional Requirements

**Base resume (Priority: P0)**
- **One file:** `profile/resume.docx` (`XC-11`). A missing or empty file raises before any row is processed, pointing to `profile.example/`.
- **Rendered to text** (pure over the loaded document; `python-docx` is approved for reading it): paragraphs in order, bold preserved as `**…**`, so the checklist agent sees a resume's structure and emphasis.

**Cover letter (Priority: P0)**
- **One optional file:** the file in `profile/` whose name matches `^cover_letter\..+$` (`XC-11`), such as `cover_letter.docx`. Absent, packets simply have none and nothing raises. More than one match raises before any row is processed, naming the files, because the app won't guess which is meant.
- **Part of the packet head:** the head (below) copies it into the packet, so `jsa packet`, `jsa generate`, and refetch's regeneration all include it through the one implementation.

**Packet directory (Step 4's head) (Priority: P0)**
- Creates `{packets_dir}/{normalized_company} - {title_slug}` (`packets_dir` from `profile/config.toml`, default `~/Documents/Job Applications`; name from PRD 01's naming fields), writes `job_posting.md` from `jd_markdown`, and copies the base resume in as `{resume_file_stem}.docx`, the user's working copy. File names are `{candidate_name}_Resume_{title_slug}_{normalized_company}` with spaces removed (pure; `candidate_name` from `profile/config.toml`); the candidate prefix is omitted when the key is unset. The cover letter copy is named `{cover_letter_file_stem}` + the source file's extension, where `cover_letter_file_stem` is `{candidate_name}_CoverLetter_{title_slug}_{normalized_company}` built by the same rule (defined once beside `resume_file_stem`).
- **Name collisions:** two postings can share a normalized company and title slug (two reqs for the same role). The two are compared without regard to case, because company capitalization is kept as the posting gave it (PRD 01) and the packets folder's filesystem may not tell "EliseAI" from "Eliseai". The lowest-id posting gets the plain directory name, and each other's directory name gets ` ({id})` appended, so a posting never re-enters another posting's packet. The rule reads only the DB, so every command and refresh derives the same name.
- **Never overwrites the resume copy or the cover letter copy:** an existing file at either path is left alone, since it may hold the user's edits. The one later step that writes to the resume copy is the redline, as tracked changes ("ATS redline" below).
- Standalone `jsa packet` **skips a posting whose directory already exists** (never clobbered) and writes no checklist, redline, or resume PDF; a `NULL`-JD row still gets its directory and resume copy (no `job_posting.md`). Queue: the packet queue (PRD 02; `--id` waives the tracker condition, never Apply).

**Placeholders in the resume and cover letter (Priority: P1)**
- **Three tokens,** each filled from the posting:

  | Token | Filled with |
  |---|---|
  | `[COMPANY]` | the posting's `normalized_company` |
  | `[TITLE]` | the posting's `title` |
  | `[DATE]` | the day the copy is made, in the profile's `timezone` (as the tracker's Date Added): the full English month name, the day without a leading zero, and the four-digit year, e.g. `October 10, 2026` |

- **The token is the request:** there is no setting. A base resume or cover letter that holds none is copied byte for byte.
- **Filled in the packet's copy, when the copy is made:** the head fills them as it copies the file in, through the one implementation every command and the inbox machine use. The files in `profile/` are never changed. A copy is never overwritten, so its placeholders are never filled a second time.
- **`.docx` only:** the resume copy, and the cover letter copy when the cover letter is a `.docx`. A cover letter in any other format is copied as it is.
- **Where:** body paragraphs, tables, headers, and footers. A token that Word has split across several runs is still filled. The filled text takes the formatting of the token's first character, and nothing else in the document changes. Text boxes aren't filled.
- **Exact match:** the token in capitals, with its brackets. `[Company]` or `[ROLE]` is left as written.
- **Filled as plain text:** the company and title come from employer pages, and are inserted as literal text, never as markup.
- **Pure (`XC-9`):** filling works over a loaded document and the three values, with no I/O.
- **Everything downstream reads the filled copy:** the checklist, the redline, and the PDF copies.

**PDF copies (Priority: P0)**
- **Converter:** LibreOffice, headless (`soffice`; `JSA_SOFFICE_BIN` override), renders a packet's `.docx` to a PDF of the same name beside it, locally and on the inbox machine (PRD 06). A failed render, or one that produces no PDF, raises; the row is flagged `failed`, and no partial PDF is left behind.
- **Safe under the worker pool:** rows are built at once (`JSA_GENERATE_WORKERS`), so one row's conversion never fails or waits because another's is running.
- **Cover letter:** a `.docx` cover letter copy always has `{cover_letter_file_stem}.pdf` beside it. The head renders it from the packet's copy, so after its placeholders are filled, whenever the copy exists and the PDF doesn't, in `jsa packet`, `jsa generate`, and refetch's regeneration alike. A cover letter in any other format, `.pdf` included, is copied as it is and nothing is rendered.
- **Resume:** `{resume_file_stem}.pdf` exists only when the redline changed nothing.
  - Generate renders it from the resume copy once the redline step ends with no edit applied: none proposed, or every one dropped.
  - When the redline applies an edit, no PDF is written, so the packet's only resume file is the redlined `.docx`.
  - No PDF is written where no redline has run: by `jsa packet`, for a row with no JD, or for a row whose redline was skipped for unresolved tracked changes.
- **Never replaced or refreshed:** an existing PDF is left alone. After the user edits a `.docx`, exporting it is theirs; the app never re-renders a PDF to follow an edit.
- **Re-entry:** `redline_edits.json` records whether any edit was applied. A run that finds it with none applied and no resume PDF renders the PDF, as a checklist missing its PDF gets one.

**Resume checklist (Step 4) (Priority: P0)**
- **Re-check before building (P0, `XC-5`):** each queued row gets PRD 01's liveness re-check before anything is built. A closed one is marked closed and gets no packet, checklist, or tracker row; generate reports how many Apply postings closed since they were decided. `--id` builds a closed posting anyway.
- **Queue:** the packet queue (PRD 02). Unlike `jsa packet`, generate **ensures** (re-enters) a bare directory (`XC-10`), completing the packet head before writing the checklist.
- **Assesses the packet's resume copy, never the base:** the checklist always reads `{resume_file_stem}.docx` as it currently stands in the packet. On a first run that copy is identical to the base; on a refresh (`--id`, refetch) it is the user's revision in progress. Tracked changes still pending in the copy are read as accepted, since the redlined copy is the one that becomes the submitted version.
- **Agent:** the Claude Agent SDK, headless, **`model` and `effort` from `[agents.checklist]` in `profile/config.toml`** (`XC-14`; example default `claude-fable-5-1` at `medium`), no tools at all, a single turn, run through the shared agent loop (`XC-12`), so an API error result raises with the real HTTP status. Its final text is written to the packet as `resume_checklist.md`; an empty result raises.
- **Wall-clock limit (optional):** `wall_clock_seconds` in `[agents.checklist]` is passed to the shared loop as its hard stop. The unattended inbox runs the checklist (PRD 03 side door, `XC-1`), and a hung CLI subprocess would otherwise bill the machine indefinitely. When it runs past the limit the run raises, the CLI subprocess ends with it, and the row is flagged `failed` and stays in the queue. A positive number; unset leaves the checklist unbounded, because the app keeps no default number of its own (the right one depends on the model and effort the user chose, `XC-14`).
- **Rendered to PDF:** `resume_checklist.md` is also rendered to `resume_checklist.pdf` with `pandoc` (`JSA_PANDOC_BIN`) through Typst. A rewritten checklist first removes the old PDF, so a failed render never leaves a stale PDF beside a new checklist, and an existing checklist missing its PDF gets one on the next run.
- **Raw blocks disabled (P0):** pandoc reads the checklist as Markdown with raw blocks off. The checklist is model output over untrusted posting text, and a raw Typst block could read a file from the machine into the PDF.
- **What the checklist is:** an assessment of the resume against this posting, from the hiring manager's seat (which strengths to lead with, which requirements it leaves unmet or under-evidenced, and what to revise), for the user to work through by hand. Its content, structure, and standards live in the prompt template; this spec owns its inputs, its output, and its boundary: it advises, and nothing in the app applies it.
- **Prompt (`XC-13`):** the app's checklist template, assembled with the slots below. It has no profile slots: the template is candidate-agnostic (`XC-11`), and the posting and the resume are its only inputs, by design.

  | Slot | Source | Required | Precedence |
  |---|---|---|---|
  | `{{JOB_TITLE}}`, `{{COMPANY}}`, `{{JOB_DESCRIPTION}}` | the posting row (or a hand-filled `job_posting.md`) | — | Input. |
  | `{{RESUME}}` | the packet's resume copy, rendered to text | yes | The document under assessment; the only evidence about the candidate. |

- **Never review blind:** a `NULL`-JD row gets its packet head but no checklist, is flagged, and stays in the queue untracked. An existing hand-filled `job_posting.md` is used as the JD and never overwritten from a `NULL` `jd_markdown`.
- **Re-entry:** the queue path writes the checklist only when the packet lacks one, so a run interrupted after the checklist resumes at the tracker append. `--id` and a refetch refresh always rewrite it, against the resume copy's current contents.
- **Concurrency & seam:** a bounded worker pool (`JSA_GENERATE_WORKERS`, default 3) with tracker appends serialized; each finished row is appended to the tracker right away, exactly as `jsa track --id` would (the Step 4→5 seam). A failed generate or track is flagged and exits non-zero; never a rollback.

**ATS redline (Step 4) (Priority: P1)**
- **Boundary (P0 whenever the redline exists):** outside the resume's skills section ("Skills section" below), the redline aligns the resume's wording with the posting's literal terms, and does nothing else. Every edit it proposes there is:
  - **traced:** it cites verbatim posting text that motivates it;
  - **meaning-preserving:** the bullet claims exactly what it claimed before;
  - **local:** it changes a phrase, not a sentence.

  An edit with no posting text behind it is never proposed, however much better it would read. Zero edits is a valid result.
- **Allowed edits:**
  - replacing a term with the posting's term for the same thing in that bullet's context;
  - pairing an acronym with its expansion;
  - using the posting's form or spelling of a tool or method the bullet already names.
- **Never allowed:**
  - adding a skill, tool, domain, scope, or outcome the bullet doesn't already state (e.g. "SQL" → "PostgreSQL");
  - changing role strength or seniority (e.g. "supported" → "led");
  - changing an employer, title, date, or number;
  - deleting content;
  - any edit made for style.
- **Part of `jsa generate`:** each row that gets a checklist then gets a redline from the same worker, before its tracker append. The redline reads the same resume copy as the checklist and works independently of it; neither sees the other's output. `jsa packet` writes neither.
- **Why it can run headless:** the user's judgment is applied where the redline is reviewed, in Word, one change at a time. Proposing the edits needs no conversation, and the validator below holds the guarantees that matter. So the redline is a batch step like the checklist, not an interactive session.
- **Agent:** the Claude Agent SDK, headless, **`model` and `effort` from `[agents.redline]` in `profile/config.toml`** (`XC-14`; example default `claude-fable-5-1` at `medium`), no tools at all, a single turn, run through the shared agent loop (`XC-12`), under the same optional wall-clock limit as the checklist, read from `wall_clock_seconds` in `[agents.redline]`. Its final text is a JSON object `{edits, skills_edits, explanation}`: `edits` is an array of `{paragraph, find, replace, jd_quote, why_same_meaning}` objects, `skills_edits` is an array of `{paragraph, find, replace, goal, jd_quote, reason}` objects ("Skills section" below), and `explanation` is a short plain-text string. `explanation` is non-empty exactly when both arrays are empty, and `null` otherwise. An empty result, text that isn't such an object, or an object that breaks the `explanation` rule raises.
- **Invalid edits are dropped, never repaired:** every proposed edit is checked against the rules below. An edit that fails is left out of the redline; the app never adjusts an edit to make it pass. `redline_edits.json`, the record of what was proposed, is written to the packet as a JSON object `{explanation, edits, skills_edits}`: the agent's `explanation`, and every proposed edit of either kind with its validation result (`null`, or the reason it was dropped). Generate's report gives each row's applied and dropped counts.
- **Validation (pure, `XC-9`):** a *word* is a whitespace-separated token with leading and trailing `.,;:!?()[]"'` stripped, compared case-insensitively. A token with no letter or digit, such as a `|` or `•` between skills, is not a word. An edit in `edits` passes only when all of these hold:
  - `paragraph` is the index of a body paragraph in the resume copy, and not one in the skills section while the skills list is in use;
  - `find` occurs exactly once in that paragraph's plain text;
  - it doesn't overlap an earlier-listed edit that passed in the same paragraph;
  - `jd_quote` is a substring of the job description, once runs of whitespace in both are collapsed to one space, with no other normalization;
  - **every word the edit inserts appears in its `jd_quote` or in its `find`.** The inserted words are the ones a word-level diff of `find` against `replace` marks as new. This puts traceability in code: an edit can only bring in the posting's own words;
  - `replace` is non-empty and differs from `find`. A pure deletion removes content;
  - `find` and `replace` contain the same digit-bearing words, so no number changes;
  - `find` is at most 6 words long. This is a first cut, to be revisited once the owner has judged real redlines;
  - `why_same_meaning` is non-empty.

- **Skills section (Priority: P1):** the one place the redline may change what the resume lists, not only how it words it.
  - **The skills list:** `profile/resume_skills.md`, optional (`XC-11`), holds the skills the user has. Items are separated by line breaks, by commas, or by both in one file:

    ```
    Python
    SQL, RDMS
    Notion
    ```

    Each item is trimmed, a leading Markdown list marker is dropped, empty items are dropped, and items that differ only by case are kept once (parsing is pure). With no file, or a file with no items, there is no skills list: the skills section is redlined under the ordinary rules above, and every `skills_edits` entry is dropped.
  - **Where the section is (pure):** the body paragraphs after the one paragraph whose whole text is `Skills` (case ignored, a trailing colon allowed), up to the next paragraph that is in a Word heading style or is wholly bold, or the end of the document. With no such paragraph, or more than one, the resume has no skills section: generate warns, naming the row, and the ordinary rules apply to every paragraph.
  - **Latitude:** in the section, the agent may add a skill, remove one, or replace one with a synonym, as it judges best for two goals in rank order:
    1. **ATS matching:** the section names the posting's skills in the posting's terms;
    2. **Consistent narrative:** the skills listed fit the story the rest of the resume tells for this posting.

    A change made for the narrative never undoes one made for ATS matching.
  - **A skills edit:** `goal` is `ats` or `narrative`. `jd_quote` is verbatim posting text, required for `ats` and `null` or a quote for `narrative`. `reason` is one sentence on why the change serves its goal.
  - **Validation (pure, `XC-9`):** which rules an edit is held to is decided by code from its `paragraph`, never by which array the agent put it in. A skills edit passes only when all of these hold:
    - `paragraph` is the index of a paragraph in the skills section, and the skills list is in use;
    - `find` occurs exactly once in that paragraph's plain text, and doesn't overlap an earlier-listed edit that passed in the same paragraph;
    - `replace` differs from `find`. It may be shorter or empty, since removing a skill is allowed;
    - `goal` is one of the two values, and an `ats` edit has a `jd_quote`;
    - a `jd_quote`, when given, is a substring of the job description under the same whitespace rule as above;
    - **every word the edit inserts appears in the skills list, in its `jd_quote`, or in its `find`.** So a skill reaches the section only from the user's own list or in the posting's own words;
    - `reason` is non-empty.

    The length limit on `find` and the number rule don't apply.
  - **What code can't check** is whether a posting's term is a true synonym for a skill the user has, or whether a removal helps the narrative. Those rest on the template's rules and on the user's accept or reject, with the reason beside each change.
- **Why no edits:** when the agent proposes none of either kind, its `explanation` says why, so an empty redline can be read rather than guessed at. Two causes are told apart: the resume already uses the posting's terms (very well aligned), or the resume and posting share so little vocabulary that no same-meaning swap exists (little overlap). The explanation is advisory, like the checklist, and nothing validates it. It is kept in `redline_edits.json` and nowhere else, since an empty redline writes nothing to the resume copy. When edits were proposed but every one was dropped, the per-edit reasons in the file are the account, and `explanation` is `null`.

  *What code can't check is meaning.* A swap worded entirely from the posting can still shift a claim (e.g. "SQL" → "PostgreSQL"). That judgment rests on the template's rules and on the user's accept or reject. The comment beside each change puts the evidence in front of the user at the moment they decide.
- **Written into the resume copy:** when at least one edit of either kind passes, the redlined document replaces the packet's resume copy under the same name, `{resume_file_stem}.docx`, so the packet holds one resume file and no PDF ("PDF copies" above). The replacement lands whole or not at all, so an interrupted run never leaves a damaged resume.
  - **Changes:** each edit is a native Word tracked change that deletes and inserts only the words that differ. The author is `Claude (ATS)`, and the date is the time of the run.
  - **Comments:** each change carries a Word comment that quotes the `jd_quote` and gives the `why_same_meaning`. A skills edit's comment names its goal, quotes its `jd_quote` when it has one, and gives its `reason`.
  - **Formatting:** inserted text takes the run formatting of the text it replaces, and every paragraph and run the edits don't touch is unchanged. So rejecting every change gives back the resume copy's text exactly, and accepting every change gives that text with the edits applied.
  - **Review:** generate doesn't open it, because it runs over a batch of rows on a worker pool. The user opens the resume copy in Word, accepts or rejects each change, and saves it. The app never does that step.
- **Reads the copy, never the base:** the redline reads `{resume_file_stem}.docx` as it currently stands, never `profile/resume.docx`. Edits the user made to the copy before the redline ran are the text under edit, and rejecting every change gives them back.
- **Never review blind:** a row that gets no checklist for want of a JD gets no redline either.
- **Re-entry:** `redline_edits.json` marks the redline step done and records whether any edit was applied; it is written after the resume copy is replaced. No command runs the redline while that file exists: not the queue path, so a run interrupted after the redline resumes at the tracker append, and not `--id` or a refetch refresh, because the copy may hold changes the user is reviewing or has already resolved. To get a fresh redline of a revised resume, the user deletes `redline_edits.json` and runs `jsa generate --id`.
- **Unresolved tracked changes in the resume copy:** the redline is skipped for that row, with a warning, and the row continues to its tracker append. The copy's paragraph text is ambiguous until the user accepts or rejects those changes, and that is the user's state to fix, not a failure.
- **Text in scope:** the body paragraphs, the same ones the checklist sees ("Base resume"). Text in tables, headers, footers, and text boxes isn't redlined.
- **Prompt (`XC-13`):** the app's redline template. It is candidate-agnostic, and its one profile slot is the skills list. It states the boundary, the allowed and forbidden edits above, the skills section's latitude and its two ranked goals, the output contract (`edits`, `skills_edits`, and `explanation`), and the two causes an empty result must choose between.

  | Slot | Source | Required | Precedence |
  |---|---|---|---|
  | `{{JOB_DESCRIPTION}}` | the posting row (or a hand-filled `job_posting.md`), as for the checklist | — | The only evidence an edit may cite. |
  | `{{RESUME_PARAGRAPHS}}` | the resume copy's body paragraphs as plain text, each prefixed with its index; empty paragraphs are omitted but keep their indices (rendering is pure, beside "Base resume") | yes | The text under edit. Edits address it by index. |
  | `{{SKILLS}}` | `profile/resume_skills.md`, parsed, one item per line | optional | The only skills, beyond the posting's own words, a skills edit may add. The boundary and the output contract override it. Absent, a fixed note says the skills section is edited under the ordinary rules. |
  | `{{SKILLS_SECTION}}` | the indices of the skills section's paragraphs, located by code; a fixed note when the resume has none or the skills list is absent | — | Tells the agent which paragraphs take skills edits, so the agent and the validator agree. |

**Tracker write (Step 5) (Priority: P0)**
- **Row shape A:H** (pure): **A** ID (`postings.id`, the join key), **B** Company (`normalized_company`), **C** Title, **D** URL, **E** Date Posted, **F** Date Added (today, in the profile's `timezone`), **G** Date Applied (blank — user-owned), **H** Status (blank — user-owned dropdown).
- **Append:** shells out to the local `gws` CLI (`JSA_GWS_BIN` override) with `valueInputOption = USER_ENTERED` and **`insertDataOption = OVERWRITE`** (never `INSERT_ROWS`). *Rationale:* `INSERT_ROWS` lands the row outside the Status column's data-validation and conditional-formatting ranges (losing the dropdown) and shifts those ranges down each time; `OVERWRITE` writes into pre-formatted blank rows. Measured on the live sheet; do not "restore" `INSERT_ROWS`.
- **Posting text is never evaluated (P0):** Company, Title, and URL come from employer pages, and `USER_ENTERED` would run any of them that looks like a formula. A cell value beginning with `=`, `+`, `-`, or `@` is written as literal text.
- **Idempotency & safety (P0):** eligibility is the tracker queue (`Apply AND added_to_tracker = 0`, PRD 02); the append **raises on any ambiguity** (non-zero `gws` exit, unparseable output, a response reporting no updated row) because optimistically flagging on exit 0 would drop a job from the tracker permanently. Rows are appended one at a time so one failure cannot strand the rest; a row is marked tracked only after a confirmed append.
- **Projection reads and writes:** rewriting a row's Title cell (`C{row}`, used by refetch); reading the tracker index (`postings.id → {row number, Date Applied}`, skipping non-integer ID cells); authority never flows Sheet→DB (`XC-4`).

**Packets built on the inbox machine (Priority: P0)** — the email side door's continuation (PRD 03), for one posting that has a description.
- **The same build, for one posting:** exactly what `jsa generate --id` does for it — packet head with its filled placeholders, checklist and its PDF, redline, PDF copies — through the same implementation, into a temporary directory on the machine. The inbox machine has the base resume, the cover letter, and the skills list as machine files (PRD 06), so its packet is identical to a local one. There is no liveness re-check, because the user vouched for the posting by sending it, as with `--id` (`XC-5`).
- **Delivered to Drive before tracking (P0):** the packet folder is uploaded through `gws`, with the owner's credential, into the Drive packets folder (`[inbox] drive_folder_id`), under the folder and file names a local build would use ("Packet directory" above). Files are uploaded as they are and never converted to Google Docs format, so the resume copy stays the exact `.docx` file, tracked changes and comments included. The tracker row is appended only after every file is confirmed uploaded, so a tracked row always has its packet. Upload planning (which file goes to which folder, under which name) is pure (`XC-9`).
- **An upload never clobbers (P0):** a retry finds the posting's folder if an earlier attempt created it, uploads only the files missing from it, and never replaces or deletes a file there, because the user may already have opened it.
- **Never rebuilt:** a posting already tracked gets nothing built or uploaded (`XC-10`). Refreshing a tracked packet stays local: `jsa generate --id` or refetch.
- **A failure leaves it untracked:** a failed build or upload leaves the posting in the tracker queue. The user's next local `jsa generate` finishes it in the mirrored folder, or re-queuing the email retries it.
- **Drive scope:** the owner's credential reaches only the files and folders the app created (Drive's per-app `drive.file` scope). So the packets folder is created once with `gws` at setup (PRD 06), and everything the inbox writes lands inside it. The same scope covers the tracker: with the inbox in use, the tracker is a copy the app made with `gws` at setup, so the credential carries no Sheets scope and can't reach the user's other spreadsheets.

**Reconciliation (Priority: P1)**
- **Scope (default):** `Apply` rows that are **absent from the Sheet OR have a blank Date Applied** — the ones where drift could still change the user's next action. The Sheet index read is *fatal* in the default scope (guessing defeats it) and best-effort under `--id`/`--all` (where it only enables Title propagation). `--all` widens to every row; `--id` targets one unconditionally.
- **Re-apply the insert rule:** re-read the ATS record (off the four, the posting page's `JobPosting` data, PRD 01); the ATS-canonical title wins and `title_slug` is re-derived (PRD 02's JD capture), except on a `manual` row, whose title is the one the user confirmed at add (PRD 03).
- **Failed fetch leaves the row *completely* untouched** (`XC-6`) — never trade a good capture for a blip; for a pulled posting the stored JD is the only surviving record.
- **Title propagation:** a corrected title on a tracked-but-unapplied row updates the Sheet Title cell; a failed Sheet write degrades to a flagged hand-fix, never blocks the reconciliation.
- **Packet refresh on drift, in place:** a title/description change on a row with an existing packet directory renames the directory, the resume copy, the cover letter copy, and their PDFs to their new names when the title changed, then regenerates the row exactly as `jsa generate --id` would, which rewrites `job_posting.md` and regenerates the checklist against the (possibly revised) resume copy. Refetch never deletes a packet directory or any file in it, and renames only, never rewrites, the resume copy, the cover letter copy, and their PDFs, because they may hold the user's revision work. So a filled `[TITLE]` keeps the old title: when the title changed and the profile's resume or `.docx` cover letter holds `[TITLE]`, refetch flags the row for a hand-fix. If a distinct directory or any of those files already exists at a new name, refetch renames nothing and flags the row. A location-only change touches nothing; refetch never creates a packet where none existed. A failed generate is flagged for a manual re-run, never rolled back.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa packet [--id] [--dry-run]`, `jsa generate [--id] [--dry-run]`, `jsa track [--id] [--dry-run]`, `jsa refetch [--id] [--all] [--dry-run]` (all local; the inbox machine builds and tracks one emailed posting at a time, above).
- First-time setup (the base resume, `gws` OAuth, the Sheet) is owned by PRD 06.
- Dry-runs preview the queue/paths/rows, make no model call, and write nothing.

**Core Experience (`jsa generate`)**
1. For each Apply+untracked row still open after the re-check: ensure the packet directory, `job_posting.md`, the resume copy, and the cover letter copy with its PDF, filling any placeholders as each copy is made.
2. Assemble the checklist prompt with the JD and the text of the packet's resume copy.
3. Run the single-turn agent; write `resume_checklist.md`.
4. Assemble the redline prompt, run its single-turn agent, validate its edits, write the passing ones into the resume copy as tracked changes, and write `redline_edits.json`. When no edit passes, render the resume PDF instead.
5. Append the tracker row via `jsa track --id`.
6. The user opens the resume copy in Word, accepting or rejecting each tracked change, and works through the checklist, revising the copy by hand. A packet with a resume PDF needed no wording changes.

**Edge Cases**
- **`NULL` JD, no hand-filled `job_posting.md`:** packet head only, no checklist, flagged; the row stays queued (never reviewed blind).
- **Missing or empty `profile/resume.docx` or `tracker_spreadsheet_id`:** raises before any row is processed, pointing to `profile.example/`.
- **Agent API error (429/500/529 outliving the CLI's retries) or an empty result:** raised by the shared loop; the row is flagged `failed` and stays in the queue.
- **Checklist or redline runs past its `wall_clock_seconds`:** raised like an agent error; the row is flagged `failed` and stays in the queue, and re-entry resumes at the step that timed out.
- **Resume copy already present:** left untouched, whichever command or refresh reaches it; the checklist assesses it as it stands.
- **Resume copy deleted by the user:** the next generate copies the base in afresh before assessing.
- **Tracker append ambiguous/failed:** row stays in the backlog; `jsa track` is the recovery path.
- **Refetch drift on a tracked-but-unapplied row:** DB updated, Title cell refreshed, packet renamed and refreshed in place; a Sheet-write failure becomes a flagged hand-fix.
- **Refetch rename target already exists:** nothing renamed; flagged.
- **An Apply posting closed before its packet was built:** marked closed, skipped, and counted in generate's report; it leaves the packet and tracker queues. `--id` builds it anyway.
- **Two postings with the same company and title:** the later one's packet directory carries ` ({id})`; neither touches the other's files.
- **A title or company beginning with `=`, `+`, `-`, or `@`:** written to the Sheet as literal text, never as a formula.
- **Standalone `jsa packet` on an existing directory:** skipped, never clobbered.
- **Redline proposes an invalid edit:** the edit is dropped and recorded in `redline_edits.json` with its reason; the valid ones are still written.
- **Redline has no valid edit:** the resume copy is untouched and gets its PDF; `redline_edits.json` is still written, so re-entry doesn't rerun it.
- **Agent proposes no edits:** the resume copy is untouched and gets its PDF; `redline_edits.json` holds two empty arrays and the agent's `explanation` of why.
- **Redline applies an edit:** the resume copy now carries tracked changes, and the packet has no resume PDF.
- **No `resume_skills.md`, or a resume with no `Skills` paragraph:** the skills section gets no special latitude; the ordinary rules apply to every paragraph.
- **A skills edit adds a word that is neither on the skills list nor in its `jd_quote`:** dropped and recorded with its reason, like any invalid edit.
- **An edit in `edits` names a skills-section paragraph, or a skills edit names a paragraph outside it:** dropped; the paragraph decides the rules, not the array.
- **A `.pdf` cover letter:** copied as it is; no placeholders filled, nothing rendered.
- **A resume or cover letter with no placeholder:** copied byte for byte.
- **LibreOffice missing or a render fails:** the row is flagged `failed` and stays in the queue; the next run renders the missing PDF.
- **The user edits a `.docx` that has a PDF beside it:** the PDF is left as it was; exporting the edited file is the user's step.
- **Two postings whose companies differ only by capitalization, with the same title:** treated as sharing a name, so the later one's directory carries ` ({id})`.
- **Redline output isn't a JSON `{edits, skills_edits, explanation}` object:** raised like an agent error; the row is flagged `failed` and stays in the queue, and re-entry resumes at the redline.
- **Resume copy with unresolved tracked changes:** redline skipped with a warning; the row is still tracked.
- **`redline_edits.json` already present on `--id` or a refetch refresh:** the redline doesn't rerun and the resume copy is left as it stands; the checklist still refreshes.
- **An emailed posting with a description:** built and tracked on the inbox machine; its packet appears locally through Drive for Desktop.
- **The same posting built locally and on the inbox machine at the same moment:** an accepted race, since the inbox wakes hourly and the user is usually away when emailing. Drive may then hold a duplicate file and the Sheet a duplicate row, both visible and fixable by hand.

-----
#### Technical Considerations
- **Where it runs (`XC-1`):** `packet`, `generate`, `track`, and `refetch` run locally, on the local disk, the local profile, and the local `gws` login. The inbox machine runs one emailed posting's build and track, with `config.toml`, `resume.docx`, and the cover letter and skills list (if any) as machine files and its own `gws` credential (PRD 06).
- **Deterministic core, agentic shell (`XC-9`):** resume rendering, resume file naming, and tracker row building are pure; the checklist is one agentic call whose output is advisory, so a non-reproducible checklist never makes the packet itself non-reproducible.
- **The redline's guarantees live in code, not in the agent (`XC-9`):** validation of both kinds of edit, skills-list parsing, locating the skills section, the plain-text paragraph rendering, and the tracked-change writing are pure and tested without a model. The proposals themselves are judged by the owner on real packets.
- **Completion guard is `added_to_tracker`, not directory-exists (`XC-10`):** `jsa generate` must re-enter a bare directory.
- **`gws` ambiguity is fatal by design:** optimistic flagging would permanently drop a job.
- **Cost:** two model calls per Apply row, the checklist and the redline (plus the checklist's per `--id` or refetch refresh), each sized by the JD and the resume; a single turn each, no iteration.

-----
#### Integration Points
- **`python-docx`** (approved) — reads the packet's resume copy for rendering. It also writes the redline: comments through its comments API, and tracked changes through the XML it exposes, so no other library is needed.
- **Microsoft Word** — where the user reviews the redline.
- **LibreOffice** (`soffice`, `JSA_SOFFICE_BIN`) — renders the resume and cover letter PDFs, locally and on the inbox machine.
- **Claude Agent SDK** (the profile's checklist and redline models), tool-less; Claude auth inherited from the environment (PRD 06).
- **Google Sheets** via the **`gws` CLI** (`JSA_GWS_BIN`): the local `gws` login, or the owner's exported credential on the inbox machine.
- **Google Drive** via `gws`, with the owner's credential (the inbox machine's packet delivery); locally, **Google Drive for Desktop** mirrors the packets folder at `packets_dir`.
- **The four ATS fetchers and the `JobPosting` fallback** (PRD 01) — refetch capture.
- **Turso** (PRD 02).

**User inputs / manual setup this subsystem requires** (consolidated in PRD 06; all local-only profile content, `XC-11`):
- **`profile/resume.docx`** — the single base resume every packet starts from.
- **`profile/cover_letter.*`** (optional) — the cover letter every packet gets a copy of.
- **`profile/resume_skills.md`** (optional) — the skills the redline may draw on in the skills section.
- **`[COMPANY]`, `[TITLE]`, `[DATE]`** (optional) — typed into the base resume or a `.docx` cover letter wherever the posting's values belong.
- **LibreOffice** (the PDF copies).
- **`gws` CLI + Google OAuth** (`gws auth login`) for Sheet writes; note the testing-status OAuth 7-day token expiry until the consent screen is published.
- **The tracker Google Sheet** (with the inbox in use, the copy made with `gws`, PRD 06) with an `Applications` tab, an A:H header, and a Status dropdown / data-validation already set up; its id as **`tracker_spreadsheet_id`** in `profile/config.toml` (no code default).
- **`candidate_name`** (the resume file-name prefix) in `profile/config.toml`, optional.
- **`wall_clock_seconds`** in `[agents.checklist]` and `[agents.redline]` in `profile/config.toml`, optional: the hard stop for that agent's run. Unset means unbounded.
- With the inbox in use: the Drive packets folder, created with `gws`, its id as `[inbox] drive_folder_id`, and `packets_dir` set to its Drive for Desktop path.
- **Microsoft Word** (redline review).

-----
#### Outstanding Questions
- **How faithful the PDF copies are (open until the owner has judged real packets).**
  - **What could go wrong:** LibreOffice lays a Word document out with its own engine, so line and page breaks can differ from Word's. A machine also substitutes any font it lacks, and the inbox machine has only the fonts its image ships.
  - **What would decide it:** the owner compares one locally built and one inbox-built resume PDF against Word's own export.
  - **What a fix would be:** shipping the resume's fonts to the inbox machine, or rendering PDFs locally only.
- **A cross-packet record of redline edits (deferred; nothing in this PRD depends on it, and nothing is built for it).**
  - **What it would be for:** the same posting term keeps being substituted across many packets. The user then changes the base resume once, rather than having it redlined in every packet.
  - **Why it's deferred:** no consumer exists yet (convention 6), and each packet's `redline_edits.json` already keeps the full record of what was proposed, so deferring loses nothing while packet directories are kept.
  - **What would decide it:** the owner scans `redline_edits.json` across 15–20 packets for recurring substitutions. If they recur, the record needs two decisions:
    - **Where it lives:** a Turso table would put resume fragments (`find`, `replace`) in the hosted DB, which `XC-11` currently forbids. The alternative is storing only the posting-side terms and their counts.
    - **What it captures:** whether it records the user's accepted or rejected outcomes, which today exist only in Word.
