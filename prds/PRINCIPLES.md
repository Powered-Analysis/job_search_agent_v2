# How We Spec: A PM Playbook for Leading Engineering and Design

Most product teams don't stall because they lack talent or ideas. They stall on ambiguity — two engineers building against different assumptions, a decision that gets re-litigated every third standup, a "final" design that turns out to contradict the data model. The cost is rarely visible in any single meeting; it accumulates as friction, rework, and the slow erosion of trust between product, design, and engineering.

The PMs on our team run a specific discipline to kill that ambiguity before it costs anything. It is not heavy process — there is no approval gauntlet, no thirty-tab template, no weekly ceremony that exists to feed itself. It is a set of writing and decision habits that make a specification *executable*: something an engineer or designer can pick up, act on without a meeting, and trust to still be true next month.

This is that playbook. It is deliberately agnostic to what we build — the habits below work whether you're shipping a consumer app, a data pipeline, or an internal tool. The through-line is simple: **a good spec removes decisions from the critical path.** Everything here serves that end.

---

## Part I — Treat your documents as a system, not a pile

The single biggest lever a PM has is not any one document — it's the *relationship between* documents. When specs form a coherent system, a team can navigate them; when they're a pile of overlapping Google Docs, every question requires archaeology.

### 1. One fact, one home

Every fact in our documentation lives in exactly one place. The output schema is defined in one spec; the storage rules for that schema live in another; the way it's rendered on screen lives in a third. None of them restate the others — they *reference* them.

This sounds obvious and is almost never done. The usual failure is well-intentioned: a PM copies the relevant detail into each doc "so the reader doesn't have to jump around." Then the detail changes, three of the five copies get updated, and now the documentation actively lies. A reader who finds the stale copy builds the wrong thing.

**How to apply it:** For every recurring fact, decide which document *owns* it. Owners define; everyone else links. When you catch yourself restating a fact you know is defined elsewhere, replace the restatement with a pointer ("the storage contract is owned by the Data spec"). The rule of thumb we repeat: *this document renders the decision, it does not reword it.*

### 2. Make ownership boundaries explicit — in writing

Every spec opens by declaring not just what it covers but what it deliberately does *not*. A schema spec will say, in so many words: "No rendering decisions — those are owned by the UI spec. No detection logic — owned by the reviewer specs." The boundary is a first-class part of the document.

This does two things. It prevents two documents from both claiming a decision (the most common source of contradictory specs), and it tells a reader exactly where to go for the thing this document won't answer. A boundary stated once, up front, saves a dozen "wait, whose call is this?" threads.

**How to apply it:** Write a real Non-Goals section and keep it specific. "Out of scope: performance tuning" is noise. "This spec defines the write path and retention; it does not define read/query patterns (see the API spec) or backup policy (open question Q-9)" is a boundary a team can actually use.

### 3. Give every cross-cutting decision a stable ID

Decisions that touch more than one document get a short, permanent identifier — a cross-cutting decision might be `XC-1`, an open question `Q4-3`. Once a decision has an ID, every affected document can reference it in a single token, and anyone can trace *why* a given design choice exists back to a single canonical statement.

The payoff compounds over time. Six weeks later, when someone asks "why don't we store the assembled report as one blob?", the answer isn't a memory or a Slack search — it's `XC-1`, stated once, referenced everywhere it matters. IDs turn institutional memory into something addressable.

**How to apply it:** The moment a decision affects two or more documents, give it an ID and a one-line canonical statement. Reference the ID from each affected spec rather than re-explaining the decision in each. This is cheap to start and enormously valuable once you have more than a handful of interlocking documents.

### 4. Specs are living documents; the archive is the git log

Our specs describe the present tense. When a decision is superseded, we rewrite the affected passages *in place* — we do not leave `[RESOLVED]` markers, dated "we used to think X" annotations, or strikethrough graveyards. A reader should be able to open any spec cold and trust that every sentence describes what we are building *now*.

The fear that stops most teams from doing this is losing the history. But the history isn't lost — it's in version control, which is a far better decision log than annotations decaying inside a live document. Separating "what is true now" (the spec) from "how we got here" (the commit history) keeps the working document clean without destroying the record.

**How to apply it:** When a decision changes, edit the spec to reflect the new reality and write the *why* into your commit message, not the document body. Reserve dated, append-only logs for things that are genuinely records — incident write-ups, decision minutes — and keep them out of the specs people build from.

---

## Part II — Sequence the work so the team can move in parallel

Decomposition is where PMs most directly control velocity. A well-sequenced set of specs lets three streams of work proceed at once; a poorly sequenced one forces everyone to wait on the same bottleneck.

### 5. Settle the contracts before the implementation

Before anyone specs *how* a subsystem works internally, we lock the **contracts at its edges** — the input it accepts and the output it produces. These are drafted and ratified first, deliberately, because every downstream design depends on them. You cannot scope the engine until you know the exact shape of what flows in and what must come out.

Contracts-first is what makes parallelism safe. Once the output schema is frozen, the team building the producer and the team building the consumer can work simultaneously against the same agreed shape, and meet in the middle with confidence. Skip this and you get the classic integration disaster: two halves built against two mental models that don't fit.

**How to apply it:** Identify the two or three interface contracts everything else hangs off — usually a data schema, an API shape, or an event format — and treat them as hard prerequisites. Nothing internal gets built until they're settled. Mark them explicitly as blockers so the sequencing is visible to everyone.

### 6. Organize work into dependency tiers, and say what can run in parallel

We group specs into tiers ordered by dependency: foundational infrastructure, then core contracts, then the engine that depends on those contracts, then operational plumbing, then the user-facing surface. Critically, each spec carries an explicit **dependency note** — what must precede it, and what can proceed alongside it.

The dependency notes are the part teams skip and the part that matters most. "The chat feature can be developed in parallel with the reviewer specs, since it consumes their output, not their internals — but integration testing needs at least one working reviewer" is a sentence that unlocks a whole workstream while honestly flagging the one place it will have to converge. That single note lets a manager staff two efforts at once without setting up a collision.

**How to apply it:** For each unit of work, write one line: what blocks it, and what it does *not* block. Be specific about the difference between "can start building" and "can finish testing" — a lot of parallelism lives in that gap.

### 7. Build behind stable interfaces so today's choices aren't tomorrow's rewrite

Wherever a component's internals are likely to change, we hide them behind a stable interface and say so explicitly: "each of these is an opaque function behind a fixed signature; the dispatcher neither knows nor cares what's inside." The v1 implementation might be the simplest possible thing, but the *seam* is designed so that swapping the implementation later is a local change, not a re-architecture.

This is how a PM buys future optionality without paying for it now. You don't build the fancy version early — you build the plain version behind an interface that the fancy version can later slot into. The spec names this intent, so engineers know which boundaries are load-bearing and which are incidental.

**How to apply it:** Ask, for each subsystem, "what is most likely to change in the next version?" Draw the interface so that change stays contained. Write down which boundaries are meant to be stable — that intent is invisible in the code and belongs in the spec.

---

## Part III — Make decisions traceable, and non-decisions honest

The difference between a spec a team trusts and one they quietly ignore is whether its decisions are *earned* — visibly reasoned, and honest about what hasn't been decided yet.

### 8. The rationale travels with the decision

We almost never state a choice without the reason beside it. Not "512 MB of memory" but "512 MB, not 256 — sized for concurrent in-flight sessions, not computation; the delta is about a dollar a month, cheap insurance against the failure mode where an out-of-memory kill drops every live session at once." The *why* is inline, at the point of the decision.

Rationale-in-place is what lets a team revisit a decision intelligently instead of either cargo-culting it or blowing it up. When circumstances change, a reader can see the reasoning, check whether it still holds, and adjust — rather than reverse-engineering intent from an artifact. It also pre-empts the endless "why did we do it this way?" thread, because the answer is already written where the question will be asked.

**How to apply it:** For any non-obvious choice, write one clause of justification next to it. If you can't articulate why, that's a signal the decision isn't ready. Pay special attention to choices that look arbitrary — those are exactly the ones someone will want to overturn later, and the ones your reasoning protects.

### 9. Track open questions in a live, self-contained list

Every unresolved decision lives in a dedicated open-questions table, separate from the specs. Each row is written to stand alone — you should be able to understand the question and everything it affects *without* opening the source document. Cross-cutting questions that touch several specs are collected once at the top rather than duplicated.

The table is a **live list, not a log.** The instant a question is answered and the answer is propagated into the specs, the row is deleted — the git history preserves it. This keeps the list to exactly what's still open, which is the only thing that makes it useful as a working queue. A resolution table that accumulates answered rows becomes an archive nobody reads.

**How to apply it:** Maintain one visible list of open questions with self-contained rows. When you resolve one, push the decision into the specs and remove the row the same day. The list's job is to answer "what's still undecided?" at a glance — protect that by keeping it ruthlessly current.

### 10. "Defer" is a legitimate, documented answer

A striking number of our open questions resolve to some form of "not yet — and here's why." "This is an empirical question we'll answer once we have real data." "This depends on a tuning corpus that doesn't exist yet; defer past the beta." A deferral is recorded as an actual decision, with its reasoning, not left as an ambiguous blank.

This matters because the alternative to an honest deferral is a *guess dressed as a decision* — a made-up threshold or a premature commitment that the team then builds against and has to unwind. Naming something as deliberately deferred tells engineering "don't block on this, don't over-build for it, we'll revisit with better information." It converts an unknown from a hidden risk into a managed one.

**How to apply it:** When you genuinely can't decide something well yet, say so explicitly and say *what would let you decide* — data from a pilot, a hire, a downstream choice. Distinguish loudly between "decided: no" and "deferred: revisit when X." Both are fine; silence is not.

### 11. Show empirical humility — don't hard-code a guess

Where a choice depends on how the real system behaves, we refuse to bake in a number we can't yet justify. Target metrics are set as *proposed first cuts* pending a pilot run. Classic best-practice "tricks" are flagged as "test this empirically before we rely on it." The spec names the belief, marks it as untested, and points to the mechanism (an eval harness, a pilot, a usability test) that will confirm or kill it.

Good PMs distinguish crisply between what they *know* and what they *believe*, and they don't let a belief harden into a load-bearing requirement without evidence. This is what keeps a spec from quietly ossifying around an assumption that was never true.

**How to apply it:** Tag your assumptions as assumptions. For each one, write down how it will be validated and when. Build the measurement mechanism as a real deliverable, not an afterthought — if a claim matters enough to design around, it matters enough to test.

---

## Part IV — Write specs that are actually executable

The habits above shape the system of documents. These shape the sentences — the concrete writing moves that make a spec something a team can build from without a follow-up meeting.

### 12. Prioritize every requirement, explicitly

Every functional requirement carries a priority label — must-ship, should-ship, nice-to-have. Not a vague "important," but a commitment about what the release cannot go out without.

Unprioritized requirements are how scope silently expands: everything reads as equally mandatory, so under time pressure the team either cuts blindly or ships nothing. Explicit priorities let engineering make the right trade-off at 4pm on a Thursday without escalating — they already know what's load-bearing and what can slip.

**How to apply it:** Attach a priority to each requirement as you write it, and be honest — if everything is a must-ship, you haven't prioritized. Revisit the labels when the timeline tightens; that's what they're for.

### 13. Specify behavior, not just feature names

Our requirements read as precise behaviors: exact field-length targets, the deterministic tie-break order for a sorted list, what happens at the boundary of a daily reset. "Sort the findings" is a wish; "sort by severity descending, then by fixability, then by a fixed category order as a deterministic tie-break" is a specification. The first invites five different implementations; the second yields one.

The discipline is to write down the thing a reasonable engineer would otherwise have to guess — and then guess *differently* from the reasonable designer sitting next to them. Precision at the point of ambiguity is the whole job.

**How to apply it:** After drafting a requirement, ask "could two competent people read this and build incompatible things?" Wherever the answer is yes, add the detail that collapses it to one reading — especially ordering, defaults, boundaries, and what happens when a value is empty.

### 14. Separate what users see from what the system stores

We rigorously distinguish display labels from stored values. The screen may say "Good to Go"; the database stores `good_to_go`. The spec states both and states which is which. The same discipline separates internal fields from user-facing ones — a reviewer's internal reasoning is captured for debugging but is explicitly "never user-visible."

Conflating these two is a subtle but expensive bug factory: someone keys logic off the display string, marketing changes the wording, and the logic breaks. Naming the wire value and the presentation value as distinct things — and saying which surfaces may touch which — prevents an entire class of downstream errors.

**How to apply it:** For any user-facing value backed by stored state, define the stored representation and the displayed representation separately. Make clear that presentation copy can change freely while the stored value is a stable contract other systems depend on.

### 15. Enumerate edge cases and failure states as first-class content

Every spec treats the unhappy paths as real requirements: the empty state, the too-large input, the timeout, the mixed-validity submission, the race at a midnight boundary. Failure modes are classified and each gets a defined behavior. A recurring principle: a component's outcomes are *never silently omitted* — the system always tells the user which state it landed in, because that's what lets them decide what to do next.

We are also honest about failure in the user-facing direction. When the system can't do something, it says so plainly ("convert your file to one of these formats") rather than misclassifying the problem to save face. And accepted risks are named as accepted risks — "per-user caps don't stop one person registering many accounts; that's a known v1 gap" — rather than papered over.

**How to apply it:** Budget as much specification effort for the edge cases as for the happy path. For each failure mode, define what the user sees and what the system records. Where you're accepting a risk rather than solving it, write that down explicitly — an acknowledged gap is a decision; an unacknowledged one is a surprise.

### 16. Write user stories for every persona — including operators and developers

Our user stories don't stop at the end user. They include the *operator* who needs to bump a limit via a database command to resolve a support ticket, and the *developer* who needs to tell a genuine system failure apart from a correct rejection in the logs. These personas have real needs that shape real requirements, and leaving them out is how you ship a product that works for users but is un-runnable in production.

**How to apply it:** Enumerate everyone who touches the system across its lifecycle — end users, operators, support, the engineers who debug it at 2am — and write stories for each. The operator and developer stories often surface requirements (auditability, override paths, observable state distinctions) that no end-user story would.

### 17. Treat cost and constraints as design inputs, not afterthoughts

Cost shows up throughout our specs as a first-class design consideration — the roughly-sevenfold saving from rejecting bad input before an expensive fan-out, the dollars-per-month of a hosting choice, the token budget of a given call. Constraints (a platform's memory ceiling, an API's rate limit, a free tier's backup policy) are named where they influence the design, not discovered in production.

A PM who folds cost and constraints into the spec makes the economics of the product legible to the team, so engineers optimize for the thing that actually matters rather than guessing. It also catches the expensive architecture before it's built, when it's still just a paragraph.

**How to apply it:** Where a design choice has a cost or bumps a hard limit, state the number. Make "what does this cost and what constrains it" a standard question you answer in the spec, not a discovery you make on the invoice.

---

## The thread that ties it together

Strip away the specifics and every habit here serves one goal: **get decisions out of the critical path.** A fact with one owner is a decision made once. A stable interface is a decision that won't have to be remade. A deferred question named as deferred is a decision not to decide yet — made deliberately, not by default. A prioritized, precise, edge-case-complete requirement is a decision an engineer doesn't have to make under pressure and guess wrong.

That is what it means for a PM to lead engineering and design well. Not to make every call yourself — to build a system of documents and decisions so clear that most calls are already made, traceable, and trusted, and the team is free to spend its judgment on the problems that genuinely need it.

None of this requires special tooling. It requires the discipline to write down what you decided, why, who owns it, and what's still open — and the rigor to keep those documents honest as the truth changes. Start with one spec. Give its facts a single home, state its boundaries, prioritize its requirements, and name its open questions. The system builds from there.
