# Job Search Agent — PRD Set

This directory is the product-requirements set for the **Job Search Agent**: a personal, mostly-autonomous pipeline that finds in-scope job postings, captures them once into a single hosted database, lets the user decide fit, and turns each *Apply* into an application packet (with a checklist for the user's own resume revision) and a tracker row — improving its own search prompt with use.

These PRDs specify the Job Search Agent that this repository builds in `src/jsa/`. They are **living documents** — present-tense, rewritten in place; the archive is the git log. They follow the writing and decomposition discipline in [`PRINCIPLES.md`](PRINCIPLES.md) and the structure in [`TEMPLATE.md`](TEMPLATE.md).

## Start here

[**00 — System Overview & Cross-Cutting Decisions**](00-overview.md) is the index: the document map, the dependency tiers, and the cross-cutting decision registry (`XC-*`) that every other PRD references by ID. Read it first. For setup, read **06** next — it holds the complete account/secret/tool checklist.

## The set

| PRD | Scope |
|---|---|
| [00 — System Overview & Cross-Cutting Decisions](00-overview.md) | Doc map, dependency tiers, `XC-*` registry |
| [01 — Agentic Job Search](01-agentic-job-search.md) | Cadence, search prompt, the three runners, output schema, canonicalization, ATS capture (Steps 1–2) |
| [02 — Data & Storage](02-data-and-storage.md) | The Turso `postings` table, idempotent insert, query surface, coverage telemetry, schema creation and change |
| [03 — Fit Review & Decisioning](03-fit-review-and-decisioning.md) | The no-LLM review loop + amend flow; the manual-add side door (Step 3) |
| [04 — Application Outputs](04-application-outputs.md) | Packets, the single base resume, the resume checklist, the tracker Sheet, refetch (Steps 4–5) |
| [05 — Dynamic Development](05-dynamic-development.md) | Search-profile refinement loop |
| [06 — Platform & Operations](06-platform-and-operations.md) | Cloud/local split, `jsa deploy`, CLI, config and `profile/` layout, and the consolidated user-setup inventory |

## Conventions

- **One fact, one home.** Each PRD's boundary is stated in its Non-Goals; shared decisions live once in the `XC-*` registry (00) and are referenced, never restated.
- **Priorities** are labeled P0 (must ship) / P1 (should ship) / P2 (nice to have) on functional requirements.
- **User inputs & manual setup** are called out in every PRD and consolidated in [06](06-platform-and-operations.md) — accounts, API keys, OAuth, and one-time seeding are flagged wherever they appear.
