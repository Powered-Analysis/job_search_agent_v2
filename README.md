# Job Search Agent

A personal job-search pipeline. A scheduled cloud search finds postings that fit your profile and
stores each one once in a hosted database. You decide Apply or Skip on each at the terminal. Each
Apply becomes an application packet (the job description, a copy of your resume, a checklist for
revising it, and an ATS redline of the resume's wording) and a row in your tracker Sheet. `jsa refine` learns from your decisions and proposes
edits to your search profile.

The product is specified in [`prds/`](prds/00-overview.md); start with `prds/00-overview.md`.

## Using this for your own search

Everything about you lives in one gitignored `profile/` directory. Only `profile/search/` ever
leaves your machine. The steps below take a fresh copy of the project to a running search, in order.

### 1. Install

```sh
git clone <this repository> && cd job_search_agent_v2
uv sync
```

### 2. Create your accounts and keys

Do this once; step 3 puts the keys in `.env`.

- **Turso** account and database: `turso db create`, then `turso db show --url` (the database URL)
  and `turso db tokens create` (the auth token).
- **Search model keys**, one for each agent your schedule uses:
  - `claude`: an Anthropic Console API key (`JSA_SEARCH_ANTHROPIC_API_KEY`);
  - `perplexity`: a Perplexity API key (`PERPLEXITY_API_KEY`);
  - `gemini`: a Google AI Studio key (`GEMINI_API_KEY`).
- **Claude for the local commands** (`generate`'s resume checklist and redline, `refine`): either
  `claude setup-token` for an OAuth token (`CLAUDE_CODE_OAUTH_TOKEN`) or an Anthropic API key
  (`ANTHROPIC_API_KEY`). Set exactly one: the CLI prefers the API key, so with both set the OAuth
  login fails with a 401.
- **Fly.io** account.
- **A Google Sheet** with an `Applications` tab. Row 1 is the header `ID`, `Company`, `Title`,
  `URL`, `Date Posted`, `Date Added`, `Date Applied`, `Status` (columns A to H). Apply a Status
  dropdown (data validation) to all of column H, so appended rows never run past the formatted
  range. Its id is the part of the Sheet's URL between `/d/` and `/edit`.

### 3. Configure `.env`

```sh
cp .env.example .env
```

Fill in the variables from step 2. `.env.example` lists every variable and says which command
needs it. Real environment variables take precedence over `.env`.

### 4. Make the profile yours

```sh
cp -r profile.example profile
```

`profile.example/` describes a fictional candidate. Replace each file with your own:

| File | What it holds |
|---|---|
| `profile/config.toml` | your name, `tracker_spreadsheet_id`, `packets_dir`, `[fly]` app and region, `[inbox]` (optional, step 7), and the model and effort for the resume checklist, the ATS redline, and refine |
| `profile/resume.docx` | your single base resume |
| `profile/cover_letter.*` | optional; your cover letter, copied into each packet (not sent to the inbox machine) |
| `profile/search/search.toml` | timezone, `run_at`, the weekly schedule, the Claude runner settings, and the verification mode |
| `profile/search/*.md` | the six fragments that tell the search who you are and what you want |

No prompt in `src/` needs an edit. If one seems to, a profile slot is missing.

### 5. Create the database tables

```sh
uv run jsa init-db
```

### 6. Set up Fly

```sh
fly auth login
fly apps create <app>
```

Put the app name and a region in `[fly]` of `profile/config.toml`. Then stage the cloud's secrets
from `.env`. The cloud runs search alone, so only these five keys go, never your local Claude
credential or machine-local overrides. The `=.` skips any key you left empty:

```sh
grep -E '^(TURSO_DATABASE_URL|TURSO_AUTH_TOKEN|JSA_SEARCH_ANTHROPIC_API_KEY|PERPLEXITY_API_KEY|GEMINI_API_KEY)=.' .env | fly secrets import --stage -a <app>
```

`jsa deploy` never sets secrets; this step is always yours.

### 7. Set up the inbox (optional)

Skip this step to deploy without the inbox. With it, an emailed posting becomes a packet in Drive
and a tracker row while your computer is off: a second hourly Fly machine reads a mailbox used for
nothing else. It lives in its own Fly app, so the search machine never holds a Google credential.

1. Create a Gmail account used only as the jobs mailbox.
2. In the Google Cloud project `gws` uses, publish the OAuth consent screen to production. While it
   is in testing status Google expires the tokens after 7 days, which silently stops the inbox.
3. Authorize `gws` twice and export each credential. Give the second login its own config folder, so
   it does not replace the first:

   ```sh
   # the jobs mailbox, with Gmail's modify scope only
   GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-jobs-mailbox gws auth login --scopes https://www.googleapis.com/auth/gmail.modify
   GOOGLE_WORKSPACE_CLI_CONFIG_DIR=~/.config/gws-jobs-mailbox gws auth export --unmasked
   # you, with Drive's per-app file scope only; it also lets the Sheets API write the files the app created
   gws auth login --scopes https://www.googleapis.com/auth/drive.file
   gws auth export --unmasked
   ```

   Each export is one line of JSON. Copy yours into `.env` as `JSA_GWS_CREDENTIALS` and the
   mailbox's as `JSA_INBOX_GWS_CREDENTIALS`, unquoted, each on one line.
4. With your own login, create the Drive packets folder and copy your tracker Sheet (a copy keeps
   its formatting and its Status dropdown):

   ```sh
   gws drive files create --json '{"name": "Job Applications", "mimeType": "application/vnd.google-apps.folder"}'
   gws drive files copy --params '{"fileId": "<tracker id>"}' --json '{"name": "Job Tracker"}'
   ```

   The per-app scope reaches only files the app created, so the folder and the copy are the ones
   the inbox can write. Set `tracker_spreadsheet_id` to the copy's id, and `[inbox] drive_folder_id`
   to the folder's. Move existing packets into the folder through Google Drive for Desktop, and set
   `packets_dir` to the folder's local path.
5. Create the Fly app and fill in `[inbox]` in `profile/config.toml` (the commented block in
   `profile.example/config.toml` shows each key):

   ```sh
   fly apps create <inbox app>
   ```

   `app` is the new app, `senders` the addresses allowed to email postings, and `drive_folder_id`
   the folder from step 4.
6. Stage the inbox app's secrets from `.env`, like the search app's. The Claude credential is
   whichever one `.env` holds, the one `jsa refine` uses; `.env` holds at most one, so the filter
   stages one. An `ANTHROPIC_API_KEY` from a Console workspace with a monthly spend limit is the
   safer choice, because the limit caps what a forged email could cost. The `=.` skips any key you
   left empty or commented out:

   ```sh
   grep -E '^(TURSO_DATABASE_URL|TURSO_AUTH_TOKEN|CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|JSA_GWS_CREDENTIALS|JSA_INBOX_GWS_CREDENTIALS)=.' .env \
     | fly secrets import --stage -a <inbox app>
   ```

### 8. Deploy

From the project root:

```sh
uv run jsa deploy --dry-run   # validate the profile and list the files that would ship
uv run jsa deploy --smoke     # build, then run one search on a throwaway machine
uv run jsa deploy             # create or update the scheduled machines
```

`--smoke` runs the day's real searches once, so it costs a day's search spend and its postings land
in your database. The scheduled machine wakes hourly, and `jsa cron` runs the day's searches once,
at or after `run_at` on a scheduled day. Run `jsa deploy` again after any code change and after you
accept a refine proposal.

With `[inbox]` set, `jsa deploy` also creates or updates the inbox app's one hourly machine, running
`jsa inbox` from the same image (a machine in one Fly app can run an image from another app in the
same organization). Every deploy sets that machine's `config.toml` and `resume.docx` as machine
files; they are never in the image or the build context. So run `jsa deploy` again after you change
your resume or `config.toml`. Before building, it checks that the profile has everything the inbox
needs and that the inbox app holds each secret from step 7; it never sets a secret. `--smoke` leaves
both scheduled machines alone.

`jsa deploy` checks the search profile exactly as the cloud will before building anything, and ships
that same directory into the image, wherever `JSA_PROFILE_DIR` points. It warns,
without stopping, when the schedule leaves hours of the week unsearched, when `run_at` is after
22:59, and when a refine proposal is pending (it will not ship).

## Setup inventory

**Accounts:** Turso (database); Fly.io (hosting); Perplexity, Google AI Studio, or an Anthropic
Console key, for each search agent you schedule; Anthropic auth for the local Claude commands. For
the inbox: a Gmail account used only as the jobs mailbox, a second Fly app, and the Google Cloud
project's OAuth consent screen published to production.

**Where each secret lives:**

| Secret | Local `.env` | Fly secrets |
|---|---|---|
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | yes | yes |
| `PERPLEXITY_API_KEY`, `GEMINI_API_KEY`, `JSA_SEARCH_ANTHROPIC_API_KEY` (each if its agent is used) | yes | yes |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` (one, never both) | yes | inbox app only |
| `JSA_GWS_CREDENTIALS`, `JSA_INBOX_GWS_CREDENTIALS` (with the inbox) | yes | inbox app only |

Fly secrets belong to one app each. Step 6 stages the search app's from the first two rows;
step 7 stages the inbox app's from the rest and the first row's Turso pair.

For what "production" means and when to run locally versus on Fly, see [PRD 06's search guidance](prds/06-platform-and-operations.md#technical-considerations).

Fly's login (`fly auth login`) stays on your machine, and so does your own `gws` login. Google
credentials leave it only as the inbox app's two exported ones, each scoped to its one job.

**Local tools:** `uv`; Google Chrome (`jsa review`); the `gws` CLI, signed in with `gws auth login`
(while the OAuth consent screen is in testing status the token expires after 7 days; publish it, or
sign in again when `jsa track` reports exit code 2); `flyctl` (`jsa deploy`); `pandoc` and `typst`
(`jsa generate` renders each checklist to a PDF); Google Drive for Desktop (with the inbox, `packets_dir` mirrors the Drive packets folder); Microsoft Word (reviewing the redline); the Claude Code CLI (the local Claude commands).

**The profile:** `config.toml`, `resume.docx`, `search/search.toml`, and the six `search/*.md`
fragments, seeded from `profile.example/` (step 4).

**Outside the profile:** the Google Sheet (step 2); with the inbox, the Drive packets folder and the
tracker copy (step 7).

**One-time commands:** `uv run jsa init-db`; with the inbox, creating the packets folder and
copying the tracker with `gws`; then `uv run jsa deploy`.

## Day to day

| Command | What it does |
|---|---|
| `jsa review` | decide Apply or Skip on each undecided posting |
| `jsa revise --id <id>` or `--last` | amend the decision and note of a decided posting; `--last` picks the one decided most recently |
| `jsa add <URL>` | add a posting you already want, decided Apply |
| `jsa inbox` | the inbox machine's entrypoint: add each posting emailed to the jobs mailbox, then build its packet, upload it to the Drive packets folder, and track it (needs `[inbox] senders` and `drive_folder_id`, `JSA_INBOX_GWS_CREDENTIALS`, and `JSA_GWS_CREDENTIALS`) |
| `jsa generate` | build each Apply posting's packet with a resume checklist and an ATS redline, then track it |
| `jsa packet`, `jsa track` | the two halves of `generate`, on their own |
| `jsa refetch` | update stored postings, tracker titles, and packets from the employer's edits |
| `jsa refine` | propose search-profile edits learned from your decisions; resolve the conflict-marked copies in `profile/refine/`, then `jsa refine --accept` or `--reject` |
| `jsa search --agent <agent> --window-hours <n>` | run one search by hand |

After `jsa generate`, revise each packet's resume copy by hand against its checklist. Open the
packet's `*_redline.docx` in Word to accept or reject each proposed wording change; every change
carries a comment quoting the posting text behind it. Save the result over the resume copy to keep it.
A redline is only written when at least one proposed change passes validation; `redline_edits.json`
records every proposal and why any was dropped. To redline a revised resume again, delete the
redline and run `jsa generate --id <id>`.

## Portability

The local commands assume macOS: `jsa review` opens Chrome through `open`, and the tracker is
written through the `gws` CLI. That is an accepted constraint, not a portability goal.

## Developing without the hosted database

Point `TURSO_DATABASE_URL` at a throwaway SQLite file and run `jsa init-db`:

```sh
TURSO_DATABASE_URL=file:dev.db uv run jsa init-db
```

Leave `JSA_SEARCH_ANTHROPIC_API_KEY` unset, so the Claude search runner uses your own Claude
login. Run the tests with `uv run pytest`; format and lint with `uv run ruff format` and
`uv run ruff check`.
