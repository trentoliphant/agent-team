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
| `patches.py` | Complete, budget-bounded review patches and their completeness proof |
| `cli.py` | Registration, scheduling, inspection, recovery |
| `companions.py` | Companion declarations, manifest pins, and anonymous pinned clones |
| `pull_requests.py` | Existing-PR adoption |

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

The review budget is 180,000 characters. The diff from the base SHA to the
candidate is sent in full when it fits. Otherwise the coordinator generates a
context-free diff (`--unified=0`). It uses that diff only if it fits and its raw bytes
parse to the same per-file headers (mode, rename, binary, and index lines) and the
same changed lines, line numbers, and no-newline markers as the full diff. Hunk
lengths come from hunk headers, so content that looks like diff syntax stays content.
Both formats are parsed before use. Each file section must be complete: metadata in
Git's order with no missing or contradictory parts, names that agree with it, and
exactly the body it implies (none for mode-only, 100%-similar rename/copy, or empty
created/deleted files; a rename/copy below 100% needs an index line and a body; otherwise a binary notice or `---`/`+++` with hunks). Hunks must
change something, have valid ranges, and be in order without overlap; unchanged lines
between hunks must line up on both sides. No-newline markers must follow the last line
of their side. The context-free patch may not contain unchanged lines. Both must
be valid UTF-8, so the reviewer receives the exact bytes that were hashed and
verified; non-UTF-8 text changes are refused rather than replaced. A malformed, mismatched, or still oversized patch blocks the review; the coordinator
never truncates a patch, drops files, or raises the budget. The review record keeps
the patch format, range, sizes, SHA-256, and file and changed-line counts. When the
compact patch is used, the reviewer prompt and the published review comment state
that surrounding context was omitted and that the reviewer must inspect the full
source in the checkout.

The first review of a run is asked to report every problem it can find and to
run the validation commands and throwaway tests against its clone. Later reviews
are re-reviews: the reviewer receives the previous review's findings and its
commit, confirms each fix, and checks what changed since. An author revision
receives the latest findings in full and one line for each earlier finding, runs
the validation commands before handing off, and answers each finding; the answers
are published as a comment with the new commit. The PR description is the
author's first report and is not replaced by later rounds.

A review that passes with minor findings starts one cleanup round, when the run
is an ordinary issue run with revision budget left. The author addresses the
minor findings and a re-review of the new commit follows. A blocking finding
there is an ordinary rejection. Minor findings from that re-review, or from a
pass with no cleanup available, are listed on the ready comment. If the author
changes nothing, the earlier passing review stands.

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
are recorded locally and summarized on GitHub; raw test logs stay local. A failed
validation is published as a comment that names the command and the failing test
identifiers only. A review finding is `blocking` or `minor`; a pass may list minor
findings, and a rejection needs a blocking one. The first commit
of a run carries the task title; each revision commit names the rejection it answers
and lists the findings addressed. Failed
validation and review findings share the bounded revision budget. Each rejection
appends a revision-history entry: the round, rejected commit, validation results,
feedback, reviewer metadata, and findings. Findings are labeled against earlier
rounds. Only exact location and request matches count as repeated. A match on the
same file alone is reported as uncertain. The commit joins `rejected_shas`, and
validation and review refuse those commits.

Exhausting the budget moves the run to `handoff`. A single SQLite write records
the rejection, the handoff text, and its pending GitHub writes (the review
comment, failure statuses, and handoff comment) in an `outbox`. The handoff comment
has one marker per run, so later handoffs and operator decisions update it in place. Every review
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

An adopted PR never merges its base, so a local repair refuses a moved PR base.
`pr update` then adopts only the base: it verifies the exact PR head and base in
a separate clone, refuses closure, retargeting, head identity changes, and head
movement, and records the new base and merge base. It retires earlier evidence
as history. It leaves the repair checkout and its uncommitted work, the
candidate, the published head, grants, provenance, budget, and history
unchanged. The later `adopt` validates and reviews against the adopted base.

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

Each changed CI observation is appended to the run's `ci_checks` with its exact
head, base, validated companion pins, evidence generation, operation, and time.
When a ready run sees CI change, the observation, the return to `ci`, and the
queued pending status are saved before GitHub is called. A failed status write
therefore loses no evidence and is retried, and a transient failure stays in the
history after CI passes. `agent_team/evidence.py` holds pure, standard-library
helpers that build and render these CI records and review, rejected-review,
superseded, and historical evidence from plain dictionaries. Rendered reviews
keep their companion pins, and historical validation failures keep their
complete feedback.

For an adopted PR, every CI read saves its observation, then checks the complete
binding again: PR head, head repository and branch, base branch and SHA,
validation configuration, companion pins, and the local candidate. This applies
to ready reconciliation, `checks`, and `ci`, including pending reads that return
and failing reads that would block. The local candidate is compared with both its
validated fingerprint and its fingerprint just before the read. Inputs can move
during the CI read. If they did, evidence is retired before the operation
returns, raises, or saves its successor. The observation stays only as history,
and no "CI changed" status is queued. Its recorded `context` is the local
fingerprint taken before the read, so an edit made during the read never
appears in the observation's head, configuration, or working state. A local commit or edit stops the run with
its contributors pending. The stopped baseline keeps the earlier fingerprint, so
validation cannot continue until contributors are declared, and the edit is never
attributed to the assigned author. Earlier pending declarations are kept.

Local movement is checked on its own whenever adopted evidence is retired, not
only when nothing else moved. If configuration, pins, head, or base move along
with a local commit or edit, the same save records the pending declarations, and
the retirement reason names both changes. Only the commit, tree, and working
state count as a contribution. Configuration movement alone never does.

Work attributed before the next validation commit, by the author's implementation
or by `--contributor` declarations, is recorded with its exact fingerprint. Only
that fingerprint is exempt. A later commit or edit, including one made during a
CI read after an earlier declaration, awaits new declarations; the earlier
declaration never covers it. A candidate already ahead of the published head is
compared with its own fingerprint from before the read.

Validation fingerprints the author checkout after the candidate commit, before any
command runs, and checks it again before accepting a pass or a failure. If it moved,
the result is kept only as history for that commit, the run stops on the earlier
fingerprint, and a commit or edit awaits declarations.
The fingerprint is saved before the clone and commands. Every run, including one
resumed after a blocked validation, stops this way before staging if attributed or
frozen work has since moved.

Movement and pin changes retire evidence before any GitHub write. One save clears
validation, review, and the readiness intent and status. The same save journals
the movement or pin notice and a pending status that revokes earlier readiness.
Review evidence for the old binding that was still queued is kept locally as
unpublished evidence. A failed status write leaves the remaining writes in the
outbox with the notification flag set. Later ticks retry them without restoring
evidence. Runs without the `github` grant queue no writes. After a pin change,
new validation, review, and `ci` for the same commit do not call `mark_ready` on
a PR that is already ready for review. The new CI record reports no readiness
change.

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
evidence only for a published candidate and with the content grant. Existing PR
adoption from outside the coordinator uses `agent-team pr`.

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
