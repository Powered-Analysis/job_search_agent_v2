## Code Conventions

These conventions belong to the owner. Agents never edit this file; invariant 3 in `docs/agentic_coding_team.md` blocks any bot commit to `docs/`. `CLAUDE.md` imports this file, so every agent loads it on every run.

`CLAUDE.md`'s own Conventions section, recorded by the team through PRs, may add conventions but never contradict these. The SA enforces both. A violation is an `align` finding, which blocks merge. Cite a convention here by number, e.g. "convention 3".

### 1. DRY

Each piece of logic, constant, list, and prompt text has exactly one definition, and everything else uses it. Pull shared logic out when a second real use appears, not in anticipation of one (see convention 6).

### 2. No library bloat

Reach for the standard library or an existing dependency first. Add a library only when it's needed for functionality, or when it measurably improves code or compute efficiency.

- **Already approved:** the libraries the PRDs name, such as `httpx`, `turso_serverless`, `markdownify`, and the Claude Agent SDK.
- **Anything else:** give a one-line justification in the PR's **New dependencies** section. The SA judges it.
- **Management:** dependencies are managed with `uv` and locked in `uv.lock`.

### 3. One operation, one approach

Before writing an operation, search for an existing implementation and use it. When an issue's work turns up two approaches to the same operation, consolidate them if that's within the issue's scope. Otherwise, raise it as a `levelup`. For example:

- every HTTP call goes through the shared `httpx` client;
- every headless agent run goes through `agent.run_agent`;
- every database access goes through the `db` module;
- errors are raised and reported the same way everywhere.

### 4. Comments

Names and structure carry most of the documentation. Comments are brief, and they're written for a technical reader who isn't a software engineer:

- explain *why* something is done, or what a non-obvious step accomplishes, in plain words;
- never narrate what the code plainly does;
- cite a PRD section or `XC-*` ID instead of restating its rationale, e.g. `# XC-8: autocommit, or every write is silently rolled back`;
- no commented-out code, and no history comments ("changed from…", "previously…").

### 5. Documents describe the present

The README, `CLAUDE.md`, and docstrings are rewritten in place as the code changes. They contain no changelogs, "resolved" notes, or dated annotations. The git log is the archive.

### 6. Build only what the PRDs specify

No speculative config options, flags, extension points, or abstractions for hypothetical future needs. This is what keeps convention 1 from tipping into over-abstraction: abstract what's actually duplicated, not what might someday be.

### 7. Formatting and linting are the tool's job

Code passes `ruff format` and `ruff check` before review, so style never reaches review as a `nit`.

- `ruff check` enables the `UP` and `DTZ` rule sets, which flag superseded Python syntax and deprecated datetime calls (convention 9).
- Ruff targets Python 3.14, the version of PRD 06's image.
- Ruff is a dev-only dependency. It earns its place under convention 2 on efficiency: it ends formatting debates in review.

### 8. Nothing private in a public repository

No secrets and no real personal data in code, fixtures, test data, or PR descriptions.

CI never has the owner's `profile/`, which is gitignored (`XC-11`). Live checks and tests use `profile.example/`, the fictional candidate PRD 06 requires.

### 9. Use what's current, not just what still works

Don't use deprecated or superseded functions, libraries, or language features, even when they still run. Things are deprecated for a reason.

- **Check the docs.** Before using a library API, check that library's current documentation for the recommended way to do it.
- **Our own warnings are blocking.** A deprecation warning raised by this repository's code is a `needs-change`.
- **When a PRD names something deprecated,** don't use it, and don't quietly substitute a replacement either. Escalate with `needs-human`, naming the deprecated item and its documented replacement. Agents can't change the PRDs, so the owner decides.
