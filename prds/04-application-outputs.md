# Application Outputs
#### tl;dr

What an Apply decision turns into: a per-job **application packet** on disk (`{packets_dir}/{company} - {title}`) holding the job description, a **working copy of the user's single base resume** (with a PDF beside it when the redline changed nothing), and a copy of the user's cover letter when they keep one (with a PDF beside a `.docx` one), plus a row appended to the **Google Sheet tracker**. The app never revises the user's base resume. Its one hand on a packet's resume copy is the **redline**, which tailors that copy to the posting, for ATS matching and for narrative and positioning, and writes what it proposes into the copy as Word tracked changes and comments for the user to accept or reject one by one, except a reorder, which is applied in place under a comment. It may reword the headline, the summary, and bullets, reorder and remove bullets, and, in the skills section, add, remove, reword, and reorder skills, drawing on a skills list the user keeps. Every proposal except a spelling or grammar fix is traced to a verbatim quote from the posting, and none changes a historical record (dates, titles, employers, accomplishments, metrics). Where the agent is unsure of a change that could matter, it leaves a comment for the user to decide instead. The redline's changes live only in that packet's copy and are never pulled forward into the base resume. The app also fills three bracketed placeholders (`[COMPANY]`, `[TITLE]`, `[DATE]`) in the packet's resume and cover letter copies from the posting. A posting that arrives through the email side door (PRD 03) has its packet built on the inbox machine and delivered to the same folder through Google Drive (`XC-1`). It also owns **refetch**, which reconciles stored postings against their ATS record when a req is edited under a stable URL. This spec covers PRD Steps 4–5 plus reconciliation.

------
#### Goals

##### Business Goals
- **One command from decision to revision-ready:** `jsa generate` takes an Apply row to a finished packet (JD, resume copy, redline) and a tracker row, on a bounded worker pool.
- **The agent proposes; the user decides.** An agent tailoring a resume from a posting alone lacks the context the user has, and can visibly overfit to the posting. So every change the redline makes is a tracked change or a comment the user accepts or rejects in Word, with the posting text and the reason beside it, except a reorder, which is applied in place under a comment because the order of bullets matters little to the user and a tracked move would bury the word-level changes in blocks of moved text, and the guarantees that matter are held in code rather than trusted to the agent: traceability to the posting, unchanged numbers, and no word that the posting, the resume, and the user's skills list don't already supply.
- **One base resume:** every packet starts from the same file, `profile/resume.docx`. No step chooses between resumes.
- **The redline tailors one resume to one posting, grounded in the posting.** It optimizes two things, in rank order: **ATS matching** (the resume names what the posting names, in the posting's terms) and **narrative and positioning** (the resume leads with, and frames the candidate around, what the posting rewards). It is not a general resume critique: every change except a spelling or grammar fix cites verbatim posting text that motivates it, and nothing it changes is pulled forward into the base resume.
- **Wording, emphasis, order, and omission may change; historical records may not.** The redline may reword the headline, the summary, and bullets; reorder and remove bullets; and reorder and reword the skills section. It never adds an accomplishment, outcome, skill, tool, domain, scope, or credential the resume doesn't already state, and never changes role strength or seniority, an employer, a job title the candidate held, a date, a degree, or a number. Claims about the candidate's preferences, beliefs, narrative, or positioning (e.g. the kind of role they are seeking) are not records and may be reworded to fit the posting.
- **Where the agent is unsure, the user decides.** A change that is both uncertain and potentially high in value, for ATS matching or for positioning, becomes a comment for the user's decision. Comments are few, because each costs the user's attention.
- **The skills section draws on the user's own list:** a skills section is a list, not a claim about one job, so the redline has more room there. It may add, remove, reword, and reorder skills, first for ATS matching and second for a consistent narrative. A skill reaches the section only from the list the user keeps or in the posting's own words.
- **The packet's resume files say whether the resume is ready:** when the redline applied nothing, the packet holds the resume as `.docx` and `.pdf`, ready to submit. When it applied anything, the packet holds one resume file, the redlined `.docx`, because that is the copy that becomes the submitted version once reviewed.
- **The tracker is a faithful, non-destructive projection:** appends never break the Status dropdown or shift its data-validation ranges (`XC-4`).
- **Idempotent outputs:** `added_to_tracker` is the single completion guard, so re-runs never double-build or double-append (`XC-10`).
- **One home for packets:** with the inbox in use, `packets_dir` is the Drive for Desktop mirror of a Google Drive folder, so a packet built locally and one built on the inbox machine land in the same place.
- **Drift never silently strands a stale packet, and never destroys revision work:** when an employer edits a req, refetch reconciles the DB and refreshes the affected packet in place.

##### User Goals
- As the job seeker, I want a per-job folder holding the JD and a copy of my resume that already leads with what this posting rewards, as changes I can accept or reject, so that the resume I submit speaks to the role without my rewriting it for each one.
- As the job seeker, I want the resume's headline, summary, bullets, order, and skills aligned with the posting's own terms and priorities, without any change to what my resume claims, so that an ATS matches it and a reader sees the fit.
- As the job seeker, I want a single tracker where the agent fills the posting columns and I own the application-state columns.

##### Non-Goals
- **Writing resume content from nothing.** The redline reworks what the resume already says. It adds no accomplishment, skill, or other record, and the user decides every change. The only text the app writes into the packet's resume copy is filled placeholders, tracked changes and comments the user can reject, and reorders, which the user undoes by hand.
- **General resume critique or advice.** The app proposes only changes grounded in the posting, plus spelling and grammar fixes. An assessment of the resume against the market, or advice the posting doesn't motivate, is not its job.
- **Improving the base resume.** The redline's changes are job-specific and exist only in the packet's copy. The user carries any of them into the base resume by hand.
- **More than one base resume, or any selection among resumes.** One base resume serves every posting.
- **Tailoring, generating, or reading the cover letter.** It is copied into the packet as the user's file; the redline never sees it. Filling its placeholders is the only change the app makes to it.
- **Posting data storage and queue queries** — PRD 02 (the packet, tracker, and refetch queues, marking tracked, and JD capture).
- **ATS fetcher shapes** — PRD 01 (refetch reuses them).
- **Application state (Date Applied, Status)** — the user's columns, never agent-written (`XC-4`).

-----
#### User Stories

**Job seeker**
- As the job seeker, I want my edits to the resume copy in a packet never to be overwritten, so that my revision work is safe.
- As the job seeker, I want a hand-filled `job_posting.md` to be honored when the ATS gave no JD, so that I can still get a redline for an unsupported platform.
- As the job seeker, I want a corrected title on an unapplied role to update its tracker cell, so that the Sheet stays accurate.
- As the job seeker, I want my headline and summary reworded to speak to this role, so that the first thing a reader sees is the fit.
- As the job seeker, I want my bullets reworded to lead with what this posting rewards, without any change to what they claim, so that my experience reads as relevant.
- As the job seeker, I want the bullets that fit this posting least removed and the rest in the order the posting values, as changes I can reject, so that a reader meets my strongest evidence first.
- As the job seeker, I want a comment, not a change, where the agent is unsure but the decision could matter, so that I make the call.
- As the job seeker, I want each redline change to show the posting text behind it, beside the change in Word, so that I can judge whether it's warranted and whether it keeps my meaning.
- As the job seeker, I want a redline that has no posting text behind it, or that brings in words neither the posting nor my resume uses, to be impossible rather than merely discouraged, so that I can trust what the redline proposes is traced.
- As the job seeker, I want the skills section revised and ordered from a list of skills I actually have, so that it matches the posting without claiming anything new.
- As the job seeker, I want a packet whose resume needed no changes to hold a PDF ready to submit, and a packet with proposed changes to hold only the copy I must review, so that I never submit the wrong file.
- As the job seeker, I want the company, title, and date filled in wherever I marked them in my resume and cover letter, so that I don't retype them for every application.

**Operator / Developer**
- As the operator, I want a failed tracker append to leave the row in the backlog (never optimistically flagged), so that no job silently drops out of the tracker.
- As the developer, I want resume rendering, file naming, and tracker row building to be pure, so they are testable while the redline stays agentic (`XC-9`).
- As the developer, I want `jsa generate` to re-enter a bare packet directory (left by `jsa packet`, an interrupted run, or a refetch rename) rather than abort, because the completion guard is `added_to_tracker`, not directory-exists (`XC-10`).

-----
#### Functional Requirements

**Base resume (Priority: P0)**
- **One file:** `profile/resume.docx` (`XC-11`). A missing or empty file raises before any row is processed, pointing to `profile.example/`.
- **Rendered for the redline** (pure over the loaded document; `python-docx` is approved for reading it): the body paragraphs as plain text, each with its index; empty paragraphs are omitted but keep their indices. A wholly bold paragraph is tagged `bold`, and a bullet paragraph ("Redline" below) is tagged `bullet`, so the agent can tell a role's header from its bullets. Tracked changes never reach this step, because the redline is skipped for a copy that holds any.

**Cover letter (Priority: P0)**
- **One optional file:** the file in `profile/` whose name matches `^cover_letter\..+$` (`XC-11`), such as `cover_letter.docx`. Absent, packets simply have none and nothing raises. More than one match raises before any row is processed, naming the files, because the app won't guess which is meant.
- **Part of the packet head:** the head (below) copies it into the packet, so `jsa packet`, `jsa generate`, and refetch's regeneration all include it through the one implementation.

**Packet directory (Step 4's head) (Priority: P0)**
- Creates `{packets_dir}/{normalized_company} - {title_slug}` (`packets_dir` from `profile/config.toml`, default `~/Documents/Job Applications`; name from PRD 01's naming fields), writes `job_posting.md` from `jd_markdown`, and copies the base resume in as `{resume_file_stem}.docx`, the user's working copy. File names are `{candidate_name}_Resume_{title_slug}_{normalized_company}` with spaces removed (pure; `candidate_name` from `profile/config.toml`); the candidate prefix is omitted when the key is unset. The cover letter copy is named `{cover_letter_file_stem}` + the source file's extension, where `cover_letter_file_stem` is `{candidate_name}_CoverLetter_{title_slug}_{normalized_company}` built by the same rule (defined once beside `resume_file_stem`).
- **Name collisions:** two postings can share a normalized company and title slug (two reqs for the same role). The two are compared without regard to case, because company capitalization is kept as the posting gave it (PRD 01) and the packets folder's filesystem may not tell "EliseAI" from "Eliseai". The lowest-id posting gets the plain directory name, and each other's directory name gets ` ({id})` appended, so a posting never re-enters another posting's packet. The rule reads only the DB, so every command and refresh derives the same name.
- **Never overwrites the resume copy or the cover letter copy:** an existing file at either path is left alone, since it may hold the user's edits. The one later step that writes to the resume copy is the redline, as tracked changes and comments ("Redline" below).
- Standalone `jsa packet` **skips a posting whose directory already exists** (never clobbered) and writes no redline or resume PDF; a `NULL`-JD row still gets its directory and resume copy (no `job_posting.md`). Queue: the packet queue (PRD 02; `--id` waives the tracker condition, never Apply).

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
- **Everything downstream reads the filled copy:** the redline and the PDF copies.

**PDF copies (Priority: P0)**
- **Converter:** LibreOffice, headless (`soffice`; `JSA_SOFFICE_BIN` override), renders a packet's `.docx` to a PDF of the same name beside it, locally and on the inbox machine (PRD 06). A failed render, or one that produces no PDF, raises; the row is flagged `failed`, and no partial PDF is left behind.
- **Safe under the worker pool:** rows are built at once (`JSA_GENERATE_WORKERS`), so one row's conversion never fails or waits because another's is running.
- **Cover letter:** a `.docx` cover letter copy always has `{cover_letter_file_stem}.pdf` beside it. The head renders it from the packet's copy, so after its placeholders are filled, whenever the copy exists and the PDF doesn't, in `jsa packet`, `jsa generate`, and refetch's regeneration alike. A cover letter in any other format, `.pdf` included, is copied as it is and nothing is rendered.
- **Resume:** `{resume_file_stem}.pdf` exists only when the redline changed nothing.
  - Generate renders it from the resume copy once the redline step ends with no proposal applied: none proposed, or every one dropped.
  - When the redline applies any proposal, a change or a comment, no PDF is written, so the packet's only resume file is the redlined `.docx`.
  - No PDF is written where no redline has run: by `jsa packet`, for a row with no JD, or for a row whose redline was skipped for unresolved tracked changes.
- **Never replaced or refreshed:** an existing PDF is left alone. After the user edits a `.docx`, exporting it is theirs; the app never re-renders a PDF to follow an edit.
- **Re-entry:** `redline_edits.json` records whether any proposal was applied. A run that finds it with none applied and no resume PDF renders the PDF.

**Generate (Step 4) (Priority: P0)**
- **Re-check before building (P0, `XC-5`):** each queued row gets PRD 01's liveness re-check before anything is built. A closed one is marked closed and gets no packet, redline, or tracker row; generate reports how many Apply postings closed since they were decided. `--id` builds a closed posting anyway.
- **Queue:** the packet queue (PRD 02). Unlike `jsa packet`, generate **ensures** (re-enters) a bare directory (`XC-10`), completing the packet head before the redline.
- **`--id`:** targets one posting unconditionally and refreshes `job_posting.md` from the row, never from a `NULL` `jd_markdown`.
- **Never build blind:** a `NULL`-JD row gets its packet head but no redline, is flagged, and stays in the queue untracked. An existing hand-filled `job_posting.md` is used as the JD and never overwritten from a `NULL` `jd_markdown`.
- **Redline, then tracker:** each row that has a JD gets its redline ("Redline" below) from the same worker, then its tracker append.
- **Concurrency & seam:** a bounded worker pool (`JSA_GENERATE_WORKERS`, default 3) with tracker appends serialized; each finished row is appended to the tracker right away, exactly as `jsa track --id` would (the Step 4→5 seam). A failed generate or track is flagged and exits non-zero; never a rollback.

**Redline (Step 4) (Priority: P1)**
- **Purpose:** the redline tailors a packet's resume copy to one posting, for two things in rank order:
  1. **ATS matching:** the resume names what the posting names, in the posting's literal terms;
  2. **narrative and positioning:** the resume leads with, and frames the candidate around, what the posting rewards.

  It is not a general resume critique. Its changes are job-specific and exist only in this packet's copy; none is pulled forward into the base resume. A change that no posting text motivates is never proposed, however much better it would read. The one exception is a spelling or grammar correction.
- **What it may propose:** four kinds of proposal. A proposal that passes validation is *applied*: written into the copy.

  | Kind | Applies to | What it does |
  |---|---|---|
  | `edit` | any body paragraph | replaces a span of the paragraph's text; written as a tracked change |
  | `removal` | a bullet outside the skills section | deletes the whole bullet; written as a tracked deletion |
  | `reorder` | a run of bullets, or the lines of the skills section | puts them in a new order; written as a move in place, not a tracked change |
  | `flag` | any body paragraph | changes nothing; leaves a comment asking the user to decide |

  Together they cover:
  - **the headline:** the line that names who the candidate is professionally (e.g. "Communications Expert for Policy Non-Profits"), reworded to optimize its title, role, keywords, domain or industry, or other features of the role. It never claims a seniority, credential, or length of experience the rest of the resume doesn't support. For the owner's own resume the template adds one fixed rule: when the role is in, or works with, a highly regulated industry (healthcare, banking, insurance, pharmaceuticals, energy/utilities), the headline gains "- Specialty in Regulated Industries";
  - **the summary,** when the resume has one: edited for this role's narrative and positioning;
  - **bullets:** wording and emphasis, which bullets stay, and the order they appear in, all for this role's narrative and positioning;
  - **the skills section** ("Skills section" below): its skills and their order;
  - **spelling and grammar,** anywhere.

  No code locates the headline or the summary: they are body paragraphs, edited under the ordinary rules, and the template tells the agent how to recognize them.
- **Boundary (P0 whenever the redline exists):** wording, emphasis, order, and omission may change, and the resume's historical records (dates, titles, employers, accomplishments, metrics) may not. A proposal is:
  - **grounded:** it cites verbatim posting text that motivates it ("Validation" below);
  - **record-preserving:** it leaves every statement of record as it was.

  **Mutable claims:** a claim about the candidate's preferences, beliefs, narrative, or positioning is not a record. The redline may change it for ATS matching and narrative (e.g. a stated wish for a people-management role, in a resume applying to an individual-contributor posting).

  Never allowed:
  - adding an accomplishment, outcome, skill, tool, domain, scope, or credential the resume doesn't already state (e.g. "SQL" → "PostgreSQL", "identified funding opportunities" → "secured funding"); the skills section's own list is the one source of additions (below);
  - changing role strength or seniority (e.g. "supported" → "led");
  - changing an employer, a job title the candidate held, a date, a degree or credential, or a number;
  - moving a bullet out of the role it belongs to;
  - any change made for style alone, and any suggestion the posting doesn't motivate.

  Zero proposals is a valid result.
- **Why it can run headless:** the user's judgment is applied where the redline is reviewed, in Word, one change at a time. Proposing needs no conversation, the validator below holds the guarantees that matter, and a `flag` hands the agent's uncertainty to the user instead of resolving it. So the redline is a batch step, not an interactive session.
- **When to flag:** a `flag` is for a change the agent is unsure of **and** that could matter a lot, in ATS or positioning terms, if made. Each flag costs the user's attention, so the template asks for few, and code holds a cap ("Validation"). A change the agent is confident of is an `edit`, `removal`, or `reorder`. A change that is unsure and unimportant is not proposed.
- **Agent:** the Claude Agent SDK, headless, **`model` and `effort` from `[agents.redline]` in `profile/config.toml`** (`XC-14`; example default `claude-fable-5-1` at `medium`), no tools at all, a single turn, run through the shared agent loop (`XC-12`), so an API error result raises with the real HTTP status. The unattended inbox runs it too (PRD 03 side door, `XC-1`), and a hung CLI subprocess would otherwise bill the machine indefinitely, so **`wall_clock_seconds`** in `[agents.redline]` is passed to the shared loop as its hard stop. When the run passes the limit it raises, the CLI subprocess ends with it, and the row is flagged `failed` and stays in the queue. A positive number; unset leaves the redline unbounded, because the app keeps no default number of its own (the right one depends on the model and effort the user chose, `XC-14`).
- **Output contract:** the agent's final text is a JSON object `{edits, removals, reorders, flags, explanation}`:
  - `edits`: `{paragraph, find, replace, goal, jd_quote, reason}` objects. `goal` is `ats`, `narrative`, or `correction` (a spelling or grammar fix). `jd_quote` is verbatim posting text, required unless `goal` is `correction`, where it is `null`. `reason` is one sentence saying how the change serves its goal and why it leaves what the resume claims as it was.
  - `removals`: `{paragraph, jd_quote, reason}` objects. `reason` says why the bullet detracts from the narrative or positioning this posting calls for.
  - `reorders`: `{paragraphs, jd_quote, reason}` objects. `paragraphs` lists the paragraph indices in their new order.
  - `flags`: `{paragraph, jd_quote, suggestion, reason}` objects. `suggestion` is the change the agent would make, not applied. `reason` says why it is uncertain and why it could matter.
  - `explanation`: a short plain-text string, non-empty exactly when all four arrays are empty, and `null` otherwise.

  An empty result, text that isn't such an object, or an object that breaks the `explanation` rule raises.
- **Part of `jsa generate`:** the redline runs on every row that has a JD, before its tracker append. `jsa packet` writes none.
- **Invalid proposals are dropped, never repaired:** every proposal is checked against the rules below. One that fails is left out of the redline; the app never adjusts a proposal to make it pass. `redline_edits.json`, the record of what was proposed, is written to the packet as a JSON object `{explanation, edits, removals, reorders, flags}`: the agent's `explanation`, and every proposal of every kind with its validation result (`null`, or the reason it was dropped). Generate's report gives each row's applied and dropped counts.
- **Bullets (pure):** a *bullet paragraph* is a body paragraph that belongs to a Word list, or whose text begins with a bullet character. One definition serves the paragraph rendering and the validator, so the agent and the validator agree on which paragraphs are bullets. A *run* is a maximal sequence of consecutive bullet paragraphs, which is one role's bullets; a role header or any other paragraph ends it. A resume with no bullet paragraph has no runs, so no removal or bullet reorder can pass.
- **Validation (pure, `XC-9`):** a *word* is a whitespace-separated token with leading and trailing `.,;:!?()[]"'` stripped, compared case-insensitively. A token with no letter or digit, such as a `|` or `•` between skills, is not a word. A `jd_quote` is *valid* when it is non-empty and a substring of the job description, once runs of whitespace in both are collapsed to one space, with no other normalization. A paragraph is *in the skills section* only when the resume has a skills section and the skills list is in use ("Skills section" below). Proposals are validated in this order, and within a kind in listed order: removals, reorders, edits, flags.

  A **removal** passes only when all of these hold:
  - `paragraph` is a bullet paragraph, and not in the skills section;
  - `jd_quote` is valid, and `reason` is non-empty;
  - it is not already removed by an earlier-listed removal, and the run keeps at least one bullet once every passing removal is applied, so a role never loses all its bullets.

  A **reorder** passes only when all of these hold. It is read as applying after the removals.
  - `paragraphs` lists at least two distinct indices that, once removed paragraphs are skipped, form one consecutive stretch of **one run**, none in the skills section; or form a consecutive stretch of paragraphs **all in the skills section**;
  - the order differs from the current one;
  - it doesn't share a paragraph with an earlier-listed reorder that passed;
  - `jd_quote` is valid, and `reason` is non-empty.

  An **edit** passes only when all of these hold:
  - `paragraph` is the index of a body paragraph that no passing removal deletes;
  - `find` occurs exactly once in that paragraph's plain text, and the edit doesn't overlap an earlier-listed edit that passed in the same paragraph;
  - `replace` differs from `find`, and is non-empty unless the paragraph is in the skills section, where removing a skill is allowed;
  - `goal` is one of the three values, and `reason` is non-empty;
  - when `goal` is `ats` or `narrative`: `jd_quote` is valid, and **every word the edit inserts appears in its `jd_quote`, in its `find`, or, in the skills section, in the skills list.** The inserted words are the ones a word-level diff of `find` against `replace` marks as new. This puts traceability in code: an edit can only bring in the posting's own words, the resume's own words, or the user's own skills;
  - when `goal` is `correction`: `jd_quote` is `null`, `find` and `replace` have the same number of words, and each word that changes is within two single-character edits of the word it replaces, so a correction can fix a word but never bring in a new one;
  - outside the skills section, `find` and `replace` contain the same digit-bearing words, so no number or date changes.

  A **flag** passes only when all of these hold:
  - `paragraph` is the index of a body paragraph that no passing removal deletes;
  - `jd_quote` is valid, and `suggestion` and `reason` are non-empty;
  - it is one of the first three flags that pass. The cap of three is a first cut, to be revisited once the owner has judged real redlines.
- **What code can't check** is meaning. A rewrite worded entirely from the bullet and the posting can still shift a claim (e.g. "supported" → "led", when the posting says "led"); a headline can claim more than the resume supports; an edit can reword a line the agent should have left alone, such as an employer or title line; and whether a posting's term is a true synonym, or a removal or a reorder helps the narrative, is a judgment. Those rest on the template's rules and on the user's accept or reject, with the quote and the reason beside each change.
- **Why no proposals:** when the agent proposes nothing of any kind, its `explanation` says why, so an empty redline can be read rather than guessed at. Two causes are told apart: the resume already reads the way the posting asks (very well aligned), or the resume and posting share so little that no grounded change exists (little overlap). The explanation is advisory, and nothing validates it. It is kept in `redline_edits.json` and nowhere else, since an empty redline writes nothing to the resume copy. When proposals were made but every one was dropped, the per-proposal reasons in the file are the account, and `explanation` is `null`.
- **Written into the resume copy:** when at least one proposal is applied, the redlined document replaces the packet's resume copy under the same name, `{resume_file_stem}.docx`, so the packet holds one resume file and no PDF ("PDF copies" above). The replacement lands whole or not at all, so an interrupted run never leaves a damaged resume.
  - **Changes:** the author is `Claude (Redline)`, and the date is the time of the run.
    - An edit is a native Word tracked change that deletes and inserts only the words that differ.
    - A removal is a tracked deletion of the whole paragraph, its text and its paragraph mark, so accepting it leaves no empty line.
    - A reorder is not a tracked change. The paragraphs are moved into the new order in the copy, with no move markup, so the changes inside them stay the only tracked changes and show as word-level edits. Edits and removals on a moved paragraph are tracked as usual.
    - A flag is a comment only.
  - **Comments:** each applied proposal carries one Word comment. An edit's names its goal, quotes its `jd_quote` when it has one, and gives its `reason`. A removal's and a reorder's quote the `jd_quote` and give the `reason`; a reorder's comment spans the moved paragraphs in their new places. A flag's states that it is a decision for the user and not a change, gives the `suggestion`, quotes the `jd_quote`, and gives the `reason`.
  - **Formatting:** inserted text takes the run formatting of the text it replaces, and a removed or moved paragraph keeps its own formatting and list membership. Every paragraph and run the proposals don't touch is unchanged. So rejecting every change gives back the resume copy's text exactly, and its order except where a reorder moved paragraphs, and accepting every change gives that text with the proposals applied. Comments never alter the text.
  - **Review:** generate doesn't open it, because it runs over a batch of rows on a worker pool. The user opens the resume copy in Word, accepts or rejects each change, answers each flag, and saves it. The app never does that step.
- **Reads the copy, never the base:** the redline reads `{resume_file_stem}.docx` as it currently stands, never `profile/resume.docx`. Edits the user made to the copy before the redline ran are the text under edit, and rejecting every change gives that text back.
- **Re-entry:** `redline_edits.json` marks the redline step done and records whether any proposal was applied; it is written after the resume copy is replaced. No command runs the redline while that file exists: not the queue path, so a run interrupted after the redline resumes at the tracker append, and not `--id` or a refetch refresh, because the copy may hold changes the user is reviewing or has already resolved. To get a fresh redline of a revised resume, or of a posting whose description changed, the user deletes `redline_edits.json` and runs `jsa generate --id`.
- **Unresolved tracked changes in the resume copy:** the redline is skipped for that row, with a warning, and the row continues to its tracker append. The copy's paragraph text is ambiguous until the user accepts or rejects those changes, and that is the user's state to fix, not a failure.
- **Text in scope:** the body paragraphs. Text in tables, headers, footers, and text boxes isn't redlined, so a headline or summary kept there is out of reach.
- **Skills section (Priority: P1):** the one place the redline may change what the resume lists, not only how it words it.
  - **The skills list:** `profile/resume_skills.md`, optional (`XC-11`), holds the skills the user has. Items are separated by line breaks, by commas, or by both in one file:

    ```
    Python
    SQL, RDMS
    Notion
    ```

    Each item is trimmed, a leading Markdown list marker is dropped, empty items are dropped, and items that differ only by case are kept once (parsing is pure). With no file, or a file with no items, there is no skills list: the skills section gets no special latitude, and every paragraph is held to the ordinary rules.
  - **Where the section is (pure):** the body paragraphs after the one paragraph whose whole text is `Skills` (case ignored, a trailing colon allowed), up to the next paragraph that is in a Word heading style or is wholly bold, or the end of the document. With no such paragraph, or more than one, the resume has no skills section: generate warns, naming the row, and the ordinary rules apply to every paragraph.
  - **Latitude:** with the skills list in use, the agent may in the section add a skill, remove one, replace one with a synonym, reorder the skills within a line, and reorder the section's lines (its categories), as it judges best for two goals in rank order:
    1. **ATS matching:** the section names the posting's skills in the posting's terms, and lists the ones the posting stresses first;
    2. **Consistent narrative:** the skills listed fit the story the rest of the resume tells for this posting.

    A change made for the narrative never undoes one made for ATS matching.
  - **What differs from the ordinary rules (the paragraph decides, never the agent):** an edit in the section may insert words from the skills list, so a skill reaches the section only from the user's own list or in the posting's own words; its `replace` may be shorter or empty, since removing a skill is allowed; and the number rule doesn't apply. Reorders there may span the section's lines and aren't confined to a run. Removals don't apply there.
  - **What code can't check** is whether a posting's term is a true synonym for a skill the user has, or whether a removal helps the narrative. Those rest on the template's rules and on the user's accept or reject, with the reason beside each change.
- **Prompt (`XC-13`):** the app's redline template. It is candidate-agnostic except for the headline rule above, which names the owner, and its one profile slot is the skills list. It states the purpose and its two ranked goals, the four kinds of proposal and what each covers (including how to recognize the headline and the summary, and that a reorder preserves the resume's original formatting and spacing), the boundary, when a flag is warranted and that flags are few, the skills section's latitude, the output contract, and the two causes an empty result must choose between.

  | Slot | Source | Required | Precedence |
  |---|---|---|---|
  | `{{JOB_DESCRIPTION}}` | the posting row (or a hand-filled `job_posting.md`) | — | The only evidence a proposal may cite. |
  | `{{RESUME_PARAGRAPHS}}` | the resume copy's body paragraphs as plain text, each prefixed with its index and, where it applies, the tags `bullet` and `bold` (a wholly bold paragraph); empty paragraphs are omitted but keep their indices (rendering is pure, beside "Base resume") | yes | The text under edit. Proposals address it by index. |
  | `{{SKILLS}}` | `profile/resume_skills.md`, parsed, one item per line | optional | The only skills, beyond the posting's own words, an edit in the skills section may add. The boundary and the output contract override it. Absent, a fixed note says the skills section is edited under the ordinary rules. |
  | `{{SKILLS_SECTION}}` | the indices of the skills section's paragraphs, located by code; a fixed note when the resume has none or the skills list is absent | — | Tells the agent which paragraphs take the skills section's latitude, so the agent and the validator agree. |

**Tracker write (Step 5) (Priority: P0)**
- **Row shape A:H** (pure): **A** ID (`postings.id`, the join key), **B** Company (`normalized_company`), **C** Title, **D** URL, **E** Date Posted, **F** Date Added (today, in the profile's `timezone`), **G** Date Applied (blank — user-owned), **H** Status (blank — user-owned dropdown).
- **Append:** shells out to the local `gws` CLI (`JSA_GWS_BIN` override) with `valueInputOption = USER_ENTERED` and **`insertDataOption = OVERWRITE`** (never `INSERT_ROWS`). *Rationale:* `INSERT_ROWS` lands the row outside the Status column's data-validation and conditional-formatting ranges (losing the dropdown) and shifts those ranges down each time; `OVERWRITE` writes into pre-formatted blank rows. Measured on the live sheet; do not "restore" `INSERT_ROWS`.
- **Posting text is never evaluated (P0):** Company, Title, and URL come from employer pages, and `USER_ENTERED` would run any of them that looks like a formula. A cell value beginning with `=`, `+`, `-`, or `@` is written as literal text.
- **Idempotency & safety (P0):** eligibility is the tracker queue (`Apply AND added_to_tracker = 0`, PRD 02); the append **raises on any ambiguity** (non-zero `gws` exit, unparseable output, a response reporting no updated row) because optimistically flagging on exit 0 would drop a job from the tracker permanently. Rows are appended one at a time so one failure cannot strand the rest; a row is marked tracked only after a confirmed append.
- **Projection reads and writes:** rewriting a row's Title cell (`C{row}`, used by refetch); reading the tracker index (`postings.id → {row number, Date Applied}`, skipping non-integer ID cells); authority never flows Sheet→DB (`XC-4`).

**Packets built on the inbox machine (Priority: P0)** — the email side door's continuation (PRD 03), for one posting that has a description.
- **The same build, for one posting:** exactly what `jsa generate --id` does for it — packet head with its filled placeholders, redline, PDF copies — through the same implementation, into a temporary directory on the machine. The inbox machine has the base resume, the cover letter, and the skills list as machine files (PRD 06), so its packet is identical to a local one. There is no liveness re-check, because the user vouched for the posting by sending it, as with `--id` (`XC-5`).
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
- **Packet refresh on drift, in place:** a title/description change on a row with an existing packet directory renames the directory, the resume copy, the cover letter copy, and their PDFs to their new names when the title changed, then refreshes the row exactly as `jsa generate --id` would, which rewrites `job_posting.md`. A finished redline is never rerun ("Redline" above), so when the description changed and `redline_edits.json` exists, refetch flags the row: the redline is traced to the old posting, and deleting the file and running `jsa generate --id` makes a fresh one. Refetch never deletes a packet directory or any file in it, and renames only, never rewrites, the resume copy, the cover letter copy, and their PDFs, because they may hold the user's revision work. So a filled `[TITLE]` keeps the old title: when the title changed and the profile's resume or `.docx` cover letter holds `[TITLE]`, refetch flags the row for a hand-fix. If a distinct directory or any of those files already exists at a new name, refetch renames nothing and flags the row. A location-only change touches nothing; refetch never creates a packet where none existed. A failed generate is flagged for a manual re-run, never rolled back.

-----
#### User Experience

**Entry Point & First-Time Experience**
- `jsa packet [--id] [--dry-run]`, `jsa generate [--id] [--dry-run]`, `jsa track [--id] [--dry-run]`, `jsa refetch [--id] [--all] [--dry-run]` (all local; the inbox machine builds and tracks one emailed posting at a time, above).
- First-time setup (the base resume, `gws` OAuth, the Sheet) is owned by PRD 06.
- Dry-runs preview the queue/paths/rows, make no model call, and write nothing.

**Core Experience (`jsa generate`)**
1. For each Apply+untracked row still open after the re-check: ensure the packet directory, `job_posting.md`, the resume copy, and the cover letter copy with its PDF, filling any placeholders as each copy is made.
2. Assemble the redline prompt with the JD, the resume copy's indexed paragraphs, and the skills list.
3. Run the single-turn agent, validate its proposals, write the passing ones into the resume copy as tracked changes and comments, and write `redline_edits.json`. When nothing passes, render the resume PDF instead.
4. Append the tracker row via `jsa track --id`.
5. The user opens the resume copy in Word, accepting or rejecting each tracked change, answering each comment, and saving it. A packet with a resume PDF needed no changes.

**Edge Cases**
- **`NULL` JD, no hand-filled `job_posting.md`:** packet head only, no redline, flagged; the row stays queued (never built blind).
- **Missing or empty `profile/resume.docx` or `tracker_spreadsheet_id`:** raises before any row is processed, pointing to `profile.example/`.
- **Agent API error (429/500/529 outliving the CLI's retries) or an empty result:** raised by the shared loop; the row is flagged `failed` and stays in the queue.
- **The redline runs past its `wall_clock_seconds`:** raised like an agent error; the row is flagged `failed` and stays in the queue, and re-entry resumes at the redline.
- **Resume copy already present:** left untouched, whichever command or refresh reaches it; the redline reads it as it stands, unless `redline_edits.json` shows the redline already ran.
- **Resume copy deleted by the user:** the next generate copies the base in afresh before the redline.
- **Tracker append ambiguous/failed:** row stays in the backlog; `jsa track` is the recovery path.
- **Refetch drift on a tracked-but-unapplied row:** DB updated, Title cell refreshed, packet renamed and refreshed in place; a Sheet-write failure becomes a flagged hand-fix.
- **Refetch rename target already exists:** nothing renamed; flagged.
- **An Apply posting closed before its packet was built:** marked closed, skipped, and counted in generate's report; it leaves the packet and tracker queues. `--id` builds it anyway.
- **Two postings with the same company and title:** the later one's packet directory carries ` ({id})`; neither touches the other's files.
- **A title or company beginning with `=`, `+`, `-`, or `@`:** written to the Sheet as literal text, never as a formula.
- **Standalone `jsa packet` on an existing directory:** skipped, never clobbered.
- **Redline proposes an invalid proposal:** it is dropped and recorded in `redline_edits.json` with its reason; the valid ones are still written.
- **Redline has no valid proposal:** the resume copy is untouched and gets its PDF; `redline_edits.json` is still written, so re-entry doesn't rerun it.
- **Agent proposes nothing:** the resume copy is untouched and gets its PDF; `redline_edits.json` holds four empty arrays and the agent's `explanation` of why.
- **Redline applies a proposal:** the resume copy now carries tracked changes or comments, and the packet has no resume PDF.
- **No `resume_skills.md`, or a resume with no `Skills` paragraph:** the skills section gets no special latitude; the ordinary rules apply to every paragraph.
- **An edit in the skills section inserts a word that is on neither the skills list, nor in its `jd_quote`, nor in its `find`:** dropped and recorded with its reason, like any invalid proposal.
- **A removal that would leave a role with no bullets, or that names a paragraph that isn't a bullet or is in the skills section:** dropped and recorded with its reason.
- **A reorder that spans two roles, or mixes the skills section with other paragraphs:** dropped.
- **A resume whose bullets are neither in a Word list nor begin with a bullet character:** no removal or bullet reorder can pass; edits and flags are unaffected.
- **A removal and an edit or flag on the same paragraph:** the removal wins, and the edit or flag is dropped.
- **More than three flags:** the first three that pass are written; the rest are dropped and recorded.
- **A correction that brings in a new word:** dropped; a correction fixes a word, never adds one.
- **Only flags applied:** the resume copy's text is unchanged but carries comments, so the packet holds the lone `.docx` and no PDF.
- **A headline or summary kept in a table or a text box:** out of reach, so not redlined.
- **A `.pdf` cover letter:** copied as it is; no placeholders filled, nothing rendered.
- **A resume or cover letter with no placeholder:** copied byte for byte.
- **LibreOffice missing or a render fails:** the row is flagged `failed` and stays in the queue; the next run renders the missing PDF.
- **The user edits a `.docx` that has a PDF beside it:** the PDF is left as it was; exporting the edited file is the user's step.
- **Two postings whose companies differ only by capitalization, with the same title:** treated as sharing a name, so the later one's directory carries ` ({id})`.
- **Redline output isn't a JSON `{edits, removals, reorders, flags, explanation}` object:** raised like an agent error; the row is flagged `failed` and stays in the queue, and re-entry resumes at the redline.
- **Resume copy with unresolved tracked changes:** redline skipped with a warning; the row is still tracked.
- **`redline_edits.json` already present on `--id` or a refetch refresh:** the redline doesn't rerun and the resume copy is left as it stands; `job_posting.md` still refreshes.
- **An emailed posting with a description:** built and tracked on the inbox machine; its packet appears locally through Drive for Desktop.
- **The same posting built locally and on the inbox machine at the same moment:** an accepted race, since the inbox wakes hourly and the user is usually away when emailing. Drive may then hold a duplicate file and the Sheet a duplicate row, both visible and fixable by hand.

-----
#### Technical Considerations
- **Where it runs (`XC-1`):** `packet`, `generate`, `track`, and `refetch` run locally, on the local disk, the local profile, and the local `gws` login. The inbox machine runs one emailed posting's build and track, with `config.toml`, `resume.docx`, and the cover letter and skills list (if any) as machine files and its own `gws` credential (PRD 06).
- **Deterministic core, agentic shell (`XC-9`):** resume rendering, resume file naming, and tracker row building are pure; the redline is one agentic call whose proposals are validated in code and reviewed by the user, so a non-reproducible redline never makes the packet itself non-reproducible.
- **The redline's guarantees live in code, not in the agent (`XC-9`):** validation of every kind of proposal, bullet detection, skills-list parsing, locating the skills section, the indexed paragraph rendering, and the tracked-change, paragraph-move, and comment writing are pure and tested without a model. The proposals themselves are judged by the owner on real packets.
- **Completion guard is `added_to_tracker`, not directory-exists (`XC-10`):** `jsa generate` must re-enter a bare directory.
- **`gws` ambiguity is fatal by design:** optimistic flagging would permanently drop a job.
- **Cost:** one model call per Apply row, the redline, sized by the JD and the resume; a single turn, no iteration.

-----
#### Integration Points
- **`python-docx`** (approved) — reads the packet's resume copy for rendering. It also writes the redline: comments through its comments API, tracked changes and paragraph deletions through the XML it exposes, and a reorder by moving paragraph elements, so no other library is needed.
- **Microsoft Word** — where the user reviews the redline.
- **LibreOffice** (`soffice`, `JSA_SOFFICE_BIN`) — renders the resume and cover letter PDFs, locally and on the inbox machine.
- **Claude Agent SDK** (the profile's redline model), tool-less; Claude auth inherited from the environment (PRD 06).
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
- **`wall_clock_seconds`** in `[agents.redline]` in `profile/config.toml`, optional: the hard stop for the redline's run. Unset means unbounded.
- With the inbox in use: the Drive packets folder, created with `gws`, its id as `[inbox] drive_folder_id`, and `packets_dir` set to its Drive for Desktop path.
- **Microsoft Word** (redline review).

-----
#### Outstanding Questions
- **How faithful the PDF copies are (open until the owner has judged real packets).**
  - **What could go wrong:** LibreOffice lays a Word document out with its own engine, so line and page breaks can differ from Word's. A machine also substitutes any font it lacks, and the inbox machine has only the fonts its image ships.
  - **What would decide it:** the owner compares one locally built and one inbox-built resume PDF against Word's own export.
  - **What a fix would be:** shipping the resume's fonts to the inbox machine, or rendering PDFs locally only.
- **Whether the code's guarantees are enough once bullets can be rewritten (open until the owner has judged real redlines).**
  - **What could go wrong:** every word an edit inserts comes from the posting, the text it replaces, or the user's skills list, and no number or date changes, but a rewrite built from those words can still shift a claim, a headline can claim more than the resume supports, and an edit can land on an employer or title line.
  - **What would decide it:** the owner reads the tracked changes and `redline_edits.json` across 15–20 packets and counts the changes rejected for altering a claim or touching a line the redline should leave alone.
  - **What a fix would be:** a limit on how much of a paragraph one edit may replace, a code check that keeps edits off wholly bold paragraphs, a lower flag cap, or tighter rules in the template.
- **A cross-packet record of redline edits (deferred; nothing in this PRD depends on it, and nothing is built for it).**
  - **What it would be for:** the same posting term keeps being substituted across many packets. The user then changes the base resume once, rather than having it redlined in every packet.
  - **Why it's deferred:** no consumer exists yet (convention 6), and each packet's `redline_edits.json` already keeps the full record of what was proposed, so deferring loses nothing while packet directories are kept.
  - **What would decide it:** the owner scans `redline_edits.json` across 15–20 packets for recurring substitutions. If they recur, the record needs two decisions:
    - **Where it lives:** a Turso table would put resume fragments (`find`, `replace`) in the hosted DB, which `XC-11` currently forbids. The alternative is storing only the posting-side terms and their counts.
    - **What it captures:** whether it records the user's accepted or rejected outcomes, which today exist only in Word.
