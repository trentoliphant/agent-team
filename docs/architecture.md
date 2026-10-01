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
| `companions.py` | Companion declarations, manifest pins, and anonymous pinned clones |

Each project registers one repository. It may also declare public companion
repositories that its validation needs (see the README). Companions are read-only
dependencies: the coordinator never pushes to them or schedules work across them.
Register multiple repositories separately to work across projects. Cross-repository
dependency scheduling is not implemented. An issue requiring undeclared sibling
checkouts must be split or handled manually; do not substitute private workspace
paths in validation commands.

## Companion repositories

Runs of a project with companions record the primary basename (`checkout`). The
author checkout is `runs/<id>/author/<basename>`, and each validation or review
directory holds `<basename>/` beside one directory per companion. Single-repository
runs keep the `runs/<id>/author` layout.

Pins come from the registration, replaced by entries in the manifest committed at
the commit being used. `git show` reads the manifest, so ignored and uncommitted
files never count. Manifest entries must name declared companions. Each pin is a
full commit SHA. Clones use HTTPS with no credential helper, no forwarded tokens,
and a fresh empty `HOME`, so no `.netrc` or user Git configuration can supply
credentials and private repositories fail. Each clone is checked out detached at
its pin and verified. Author and review agents receive the companion checkouts as
readable directories (Claude `--add-dir`; Codex sandboxes already read outside the
workspace). Their tool sets and permission modes are unchanged.

Before each implementation round, earlier author companion checkouts, and any
other directories beside the author checkout, move aside as
`companion-preserved-*`. Every companion is then cloned again, so the coordinator
never runs Git in a directory the author could edit. Validation records the pins
(`validated_companions`) and caches the manifest entries for that commit before
running tests. Review uses the same pins and records them in the review record.
Publication (before any push or PR write), the review stage, the `ci` stage, and
ready reconciliation compare the current pins with the recorded ones. A difference
sets a pending status on an existing PR and returns the same commit to validation. Review comment markers include a pin digest, so an
earlier review of that commit stays published.

Partial selections apply the same checks. Declared companions and the manifest
path are part of the candidate fingerprint's configuration (omitted when unset),
and manifest pins are part of its tree. Continuation and every evidence-consuming
selected stage compare pins before reusing validation or review. A difference
moves the old verdict to `superseded_evidence`, records an evidence invalidation,
and stops the run with `validate` as the next stage; the stage is not recorded as
performed. A pending status is written only with the `github` grant. Operation
history and `checks` snapshots record the pins in effect. Unpublished refresh
builds its clone in the run root, so the author directory keeps only the
basename checkout and its companions.

After each validation command, and after review, each companion checkout must
still be a real directory with its original Git configuration and HEAD at the pin.
Its working tree, outside `.git`, must match the snapshot taken right after the
fresh checkout: every path, file type, executable bit, content hash, and symlink
target. The snapshot is read from the filesystem, not through Git, so index flags
(`assume-unchanged`, `skip-worktree`) and ignore rules cannot hide edited or added
files. Its `.git` directory, except the index, must match too. Replacement refs,
grafts, shallow files, alternates, and added objects or refs therefore reject the
evidence, even if removed after use. Replacement refs and grafts are also checked
by name. Staged entries must still name the original objects. Otherwise the stage
blocks and its results are not accepted. Configuration is checked before Git runs
there. Coordinator Git reads of companions and manifests use `--no-replace-objects`.

Pins are compared before a saved verdict or failed validation is reused after an
interruption, including in `refresh` and `adopt`. Evidence gathered with other
pins does not reject the commit or use a revision. It moves to
`superseded_evidence` with its pins, and the commit needs new validation and review.

## Durable transitions

Every tick saves its intended stage before doing work. If the process disappears,
the next tick after acquiring its repository lock moves the run to blocked and requires explicit resume. Finished
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

Workers share an advisory administrative gate and hold an exclusive lock for the
case-insensitive registered GitHub repository identity. A bounded set of slot
locks limits concurrent ticks (`configure --concurrency`, default 1). Multiple
registrations share repository exclusion and durable issue claims. Active work
stays with the original registration. Global configuration (concurrency, project
registration/configuration, and writing changes) takes the gate exclusively and
requires all workers to be idle. Run recovery and decisions, adoption, refresh,
approvals, queue edits, and label setup take the shared gate and the affected
repository lock without consuming a slot. Discovery takes the same locks as a
worker, including a bounded slot, across its model call and publication. Pause uses a short independent
SQLite transaction and takes effect between ticks. Watch releases its locks
between ticks and polls on contention; read-only status remains available.

SQLite write transactions serialize assignment rotation and duplicate-claim
checks across repository aliases. Run updates and journal entries commit together.
Configuration field updates read the current project inside their transaction,
so a concurrent pause cannot be overwritten by an older project snapshot.
Locks remain on disk and must never be unlinked while the directory is in use.
OS lock ownership, rather than PID checks or elapsed time, distinguishes live
workers from interrupted in-flight stages. Interrupted work still requires an
explicit resume after inspection; no automatic subscription replay occurs.

Subscription calls take a separate per-family lock. A quota error persists a
family cooldown shared across workers. Busy families and cooldowns defer runs
without calls or quota attempt increments. Actual quota failures retain bounded
retries. Diagnostic smoke calls serialize and respect existing cooldowns but
never persist a cooldown on failure. Other families and stages remain available. Optional status rewrites
retain their one-attempt fallback when capacity is unavailable.

This coordination boundary is one host and one shared local state directory.
Independent directories, remote filesystems, GitHub repository aliases caused by
renames, and mixed coordinator versions are outside the boundary. Stop all workers
before migration or backup.

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

This release does not provide a web UI, multi-host leases,
GitHub Projects synchronization, automatic semantic issue deduplication,
general-purpose plugin loading, autonomous prioritization, or automatic merge.
GitHub issues, comments, PRs, and commit statuses are the shared record; SQLite
holds resumable execution details and local logs.

## Issue ordering

Projects store `queue_order` in their existing SQLite configuration. Queue edits
use the affected repository lock and shared administrative gate. Inspection reads open issues and matching approvals
without publishing changes. Eligible listed issues precede unlisted issues by
creation time (issue number breaks ties). Explicit selection does not rewrite
the queue. Existing active runs take priority; blocked, quota-waiting, handoff, repair, and stopped runs
prevent new assignments. Targeted ticks reject conflicting work and limit
reconciliation, notifications, and execution to the selected issue. Interrupted
stages still require recovery. Completed runs retain their unique repository-wide issue claim.

## Partial selections

`select` persists immutable task or approved-issue scope, ordered operations,
separate effect grants, and the endpoint before execution. Task runs store a NULL
issue and use the same rotation, repository locks, isolated clones, stage journal,
quota handling, and revision history as issue runs. The registry migration that
permits NULL issues is transactional. Stop all workers before upgrading.

Discovery records a read-only proposal report; issue preparation records local
issue drafts. Validation can enter from a remote branch or commit without an
author pass. Publication, review, and readiness consume compatible tracked
validation/review evidence. Review can run locally without a PR. It writes GitHub
evidence only for a published candidate and with the content grant.

`pr review|revise|findings` adopts an existing PR as a selected run with no
issue (rules are in the README). Its published commit is the PR head; the base is
never merged. Revision modes store `pr_followup`, entered by a rejection within
the budget and released by `select --run`. A failed validation of the unedited
head stays pending (`attempted_context`) until review, and one rejection records
both. Unestablished independence records `review_withheld`, not `reviewed_sha`.
Movement retires evidence to `evidence_invalidations`, moves queued writes to
`unpublished_evidence`, and stops the run as `stale`. Re-adoption inherits the
round, limit, and `rejected_shas` of `prior_runs`. `pr update` journals its swap
(`pending_pr_update`) before renaming, so a rerun completes it.

Selections record cumulative requested/performed/unperformed operations and each
continuation segment. Completed endpoints and rejections stop before successor
work. A stopped selection holds the repository, including other registrations,
and blocks queue work and new selections across restarts. Explicit continuation
reuses the run; `close RUN_ID` releases the repository while preserving work and
history and leaving GitHub issues and PRs open. Closing is terminal, so the same
issue or task scope cannot restart the run. Close finished partial work only when
no continuation is planned.
Selected extension/adoption returns to a stop boundary. Failed evidence,
contributor declarations, and revision limits survive re-entry. Identical task
scope cannot be recreated to discard history, and rejected input commits cannot
be imported into new runs. Local handoff edits require declared contributors;
human-only edits are never labeled as work by the assigned model family. When an
author pass has left uncommitted work, its family attribution survives a human
handoff until the candidate commit records both contributors.

Candidate fingerprints include working content, tracked trees and pins, exact
HEAD/base, immutable scope, and execution configuration. Evidence-consuming stages
check these again. Unpublished refresh merges in a fresh clone, journals the
checkout swap, preserves previous work, and explicitly returns to validation.
A later refresh reconciles a journaled swap without repeating integration.
Separate effect grants gate local edits, push, GitHub content/status writes, and
readiness. Legacy queue execution retains its established authorization contract.

The `checks` operation records one GitHub CI snapshot for the tracked published
candidate. It does not require or manufacture a review verdict and does not
change readiness. The separate `ci` operation retains exact validation, independent
review, and readiness prerequisites. Publication follows the selected successor,
so an already reviewed local candidate can enter `publish ci` without a new review.
Continuation segments retain their grants and effects alongside their revisions.
