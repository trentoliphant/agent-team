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
| `writing.py` | Writing-standard precedence, validation, and prompt text (style only); the coordinator applies the `status` word target to its status comments |
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
branches make retries idempotent in a single coordinator installation.

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
validation and review findings share the bounded revision budget. Exhausting that
budget requires human attention. Explicit refresh starts new validation and review
even if the old budget was exhausted; it does not silently authorize unlimited
automatic revisions.

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
