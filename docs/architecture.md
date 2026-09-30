# Architecture

The coordinator is a deterministic state machine around external command-line
programs. It uses Python's standard library only. No target repository imports
this package or needs its workflow files.

## Components

| Module | Responsibility |
| --- | --- |
| `state.py` | SQLite registry, assignment rotation, run journal, process lock |
| `process.py` | Argument-vector execution, timeout/process-group cleanup, environment filtering |
| `agents.py` | Subscription authentication checks, official CLI arguments, report contracts |
| `github.py` | GitHub API reads and coordinator-owned writes through `gh` |
| `writing.py` | Writing-standard precedence, validation, and prompt text (style only); the coordinator applies the `status` policy to its status comments |
| `coordinator.py` | State transitions, independent review, Git publication, discovery |
| `cli.py` | Registration, scheduling, inspection, recovery |

Each project registers one repository. Register multiple repositories separately
to work across projects. Cross-repository dependency scheduling and coordinated
suite checkouts are not implemented in 0.1.0. An issue requiring undeclared sibling
checkouts must be split or handled manually; do not substitute private workspace
paths in validation commands.

## Durable transitions

Every tick saves its intended stage before doing work. If the process disappears,
the next tick moves the run to blocked and requires explicit resume. Finished
stages are saved before publishing the issue status comment. A pending-notification
flag lets a later tick retry a failed comment update. Comment markers and PR head
branches make retries idempotent in a single coordinator installation. Bodies
over 60,000 characters continue in separately marked comments instead of being
truncated; a shorter update marks leftover parts as unused.

Status comments come from fixed templates. When writing settings customize the
shared or `status` instructions, the run's author agent rewrites the template
through a read-only `status` report. The coordinator appends the compact
template unchanged and caches the draft per update. Before invoking the agent it
persists an attempted record; failures (including quota), empty output, and
interrupted attempts consume the single attempt and reuse the template fallback.
Thus repeated notification ticks or process restarts cannot repeat the same
status call. Changed template facts or effective policy define a new update.
No draft is attempted while waiting for quota. Codex status calls explicitly
allow a non-Git artifacts workspace while retaining the read-only sandbox.

GitHub and SQLite do not share a transaction. A crash during publication can
leave a pushed branch or PR before the local record catches up. Inspect the
run and remote branch before resuming. Existing PRs on the same branch are reused;
the pending push SHA is journaled before publication, so a lost response after
a successful push can be reconciled without treating it as an external edit.
Unexpected remote heads require explicit refresh. The coordinator does not
force-push or reset the author's checkout.

One global advisory process lock serializes mutating commands on a single host.
Pause is stored immediately and takes effect after the current stage finishes.
Read-only status works during a run. Other mutating commands fail with a clear
busy message if they cannot acquire the worker lock.
The watch loop releases the lock between ticks. A service manager may restart a
watch process, but interrupted agent work still requires explicit recovery.

## Review independence

Family is assigned by the adapter (`codex` = OpenAI, `claude` = Anthropic), never
by an agent's self-description. Each review uses a separate clone at a detached,
exact candidate commit and a fresh CLI session. It receives the issue, diff,
source, and coordinator validation results, not the author's private transcript.
Reviewer modifications invalidate the report. A passing report with findings is
rejected as ambiguous.

Reviews are explicitly committed to a SHA. The base SHA is recorded too. New
remote head/base changes make the run stale and invalidate readiness without
blocking new issue intake. `refresh`
adopts those changes only when requested, preserving old work and rerunning
validation and review. CLI metadata is reported without inventing unavailable
model identifiers. Cross-family review reduces shared context; it does not prove
correctness or eliminate correlated model errors.

## Validation and limits

The candidate is committed locally and cloned into a fresh validation directory.
Configured commands execute sequentially there, without ignored files or caches
from the author checkout. Their results
are recorded locally and summarized on GitHub; raw test logs stay local. Failed
validation and review findings share the bounded revision budget. Each rejection
appends a revision-history entry: the round, rejected commit, validation results,
feedback, reviewer metadata, and findings. Findings are labeled against earlier
rounds. Only exact location and request matches count as repeated. A match on the
same file alone is reported as uncertain. The commit joins `rejected_shas`, and
validation and review refuse those commits.

Exhausting the budget moves the run to `handoff`. A single SQLite write records
the rejection, the handoff text, and its pending GitHub writes (the review
comment, failure statuses, and handoff comment) in an `outbox`. Every review
outcome is saved this way before any GitHub write is attempted. Notification
ticks publish the outbox with idempotent marked comments. A crash or GitHub
failure therefore delays publication but never loses or duplicates it, and
never keeps a rejected run from reaching `handoff`. A review verdict is saved
with its commit before its outcome is recorded. If an interruption happens
between them, `resume` reuses that verdict instead of calling the reviewer again.
A failed validation is likewise saved with its commit, results, and output before
the rejection is recorded, and `resume` records that saved failure instead of
running validation again, so a nondeterministic command cannot replace it.
`refresh` and `adopt` first record such a pending rejection and stop, so they
cannot discard it and send the same commit to review or validation again. The run then revises
within the limit or hands off. Closed and merged runs are terminal: `refresh`
refuses them and a pending rejection is never recorded on them, so a closed run
cannot be reopened for more model work. The handoff
save also clears the in-flight marker, and crash reconciliation keeps a run in
`handoff` or `repair` rather than blocking it, so decisions stay available.

Handoff and repair runs are recovery states. They stop new intake and refuse
`resume` and `refresh`. `decide` records one of four operator decisions, with an
optional note, and queues its comment the same way:

- `extend`: 1-3 more revisions counted from the handoff round. The first
  handoff freezes the run's `revision_limit`; an extension sets it to the
  handoff round plus the authorized count. Later `max_revisions` changes do not
  affect it. Each decision records the resulting limit, and `extension` records
  the cumulative authorized amount.
- `repair`: move to `repair`. If validation exhausted the limit before anything
  was published, `decide` first checks the author checkout's recorded Git
  metadata, then clones it at the rejected commit into a local repair checkout
  and fingerprints that checkout's metadata. Changed author metadata is refused
  before any Git command runs. The author checkout is not touched. An
  interruption before the save leaves only an unused clone.
- `rescope`: close locally.
- `stop`: close locally.

`rescope` and `stop` are also accepted in `repair`, and in `stale` once the run
has a recorded decision or adoption, so an unadoptable head can still be closed
with a recorded decision and comment.

`adopt` accepts a repaired PR head in `repair`, or in `stale` once the run has
a recorded decision or adoption (for example, after an extension). In those runs,
`refresh` refuses a changed head, so every external head goes through `adopt` with
declared contributors; trailers alone never suffice. Decisions and the extension
are kept. The head must not be a
rejected commit, and it must descend from the last published candidate
(`merge-base --is-ancestor`). A force-pushed head that drops that history is
refused before the checkout swap, and the run stays in its recovery stage. The run's contributing families are the author's family, the
declared contributors, and any `Agent-Family` trailers between base and head.
If the reviewer's family is among them, adoption is refused. Review also
enforces this check. Adoption reuses refresh integration, then requires new
validation and review under the same bound. Every refresh applies the same trailer
check. Explicit refresh of a stale run starts
new validation and review; it does not silently authorize unlimited automatic
revisions.

For an unpublished candidate, `adopt` takes the committed HEAD of the local
repair checkout instead of a PR head. It refuses uncommitted changes, changed Git
metadata, a rejected commit, and a HEAD that does not descend from the rejected
commit, so history is extended, never rewritten. The same contributor and trailer
checks apply; trailers on commits from the base are excluded. Like remote
adoption, it fetches the current registered base and merges it into a clone of
the repair. On a conflict, adoption is refused and the repair checkout is kept;
merge the base there and adopt again. The run records the merged candidate and
the new base commit. That candidate replaces the author checkout (the old one is
preserved) and enters `validate` in the next round. Nothing is pushed until it
passes validation. It is then published as a draft PR, like any candidate, and
reviewed as that exact commit before it can be ready. The adoption comment goes to
the issue, and the PR body lists adopted repairs and their contributors. A crash
after the checkout swap but before the save leaves the run in `repair`; adopting
again is safe.

Candidate SHA and tree are checked again before publication. Git configuration,
excludes, and local attributes are fingerprinted; changes stop orchestration
before further coordinator Git calls. Revision requests that produce no new
commit cannot reroll a rejected review. Review comments include the round so
explicit revalidation does not overwrite earlier verdicts.

Issue approval is an explicit command that posts the current content fingerprint
and applies the ready label. Intake requires a matching comment from the current
coordinator identity. The assigned issue snapshot is immutable; later title/body
edits stop execution. Quota detection uses structured failure messages rather than
arbitrary transcript text, and consecutive quota retries are bounded.

GitHub checks and commit statuses are inspected before marking ready. Pending
checks wait, failures block, and more than 100 results stop for manual inspection.
Repositories with no GitHub checks still require local validation and agent
review. Branch protection remains the authoritative merge-time CI gate, including
checks that appear after the coordinator's poll.

This release does not provide a web UI, multi-host leases, parallel workers,
GitHub Projects synchronization, automatic semantic issue deduplication,
general-purpose plugin loading, autonomous prioritization, or automatic merge.
GitHub issues, comments, PRs, and commit statuses are the shared record; SQLite
holds resumable execution details and local logs.

## Issue ordering

Projects store `queue_order` in their existing SQLite configuration. Queue edits
use the coordinator lock. Inspection reads open issues and matching approvals
without publishing changes. Eligible listed issues precede unlisted issues by
creation time (issue number breaks ties). Explicit selection does not rewrite
the queue. Existing active runs take priority; blocked, quota-waiting, handoff, and repair runs
prevent new assignments. Targeted ticks reject conflicting work and limit
reconciliation, notifications, and execution to the selected issue. Interrupted
stages still require recovery. Completed runs retain their unique issue claim.
