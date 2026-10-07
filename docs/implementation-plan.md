# Reliable autonomous preview (0.2)

This version follows the maintainer's priorities, in order: human issue approval
and human merge authority; reliable results; useful development history;
autonomous issue-to-PR execution; runtime; subscription consumption; optional
operator tuning of run limits.

The audit found that model time, repeated validation, fixed polling delays and
optional prose/cleanup calls dominate execution. Local Python execution is not
the limiting factor. Keep Python 3.11 and the standard library, subscription-only
CLIs, family separation, fresh exact-commit validation/review, durable recovery,
and bounded retries. No merge operation will be added.

## Planned changes

1. Human approval: support explicitly configured human GitHub approvers so a
   bot coordinator can consume their approval. Require GitHub User identities
   for approval evidence and reject bot approval before writes. Provide a
   read-only approval template for humans to post. Preserve content fingerprints
   and the ready label. Shared human credentials cannot prove who operated them;
   document dedicated coordinator identities and GitHub branch rules.
2. Reliability: reject replacement refs, grafts, shallow history and alternates
   in worker Git metadata. Protect the complete review Git metadata against
   mutation, allowing the reviewer's throwaway working-tree files. Maintain
   fresh snapshots and existing publication/CI race protections.
3. Trace: add a local, read-only human/JSON trace command covering the candidate,
   all recorded author and review rounds, unresolved findings, configured test
   results, timing and artifact locations. Retain exact evidence and original
   model reports; never infer that unanswered findings were fixed. Stream model
   and validation subprocess output to artifact files so interruption retains
   partial logs. Capture provider-reported usage and call duration when available.
4. Autonomy: new registrations allow four bounded correction rounds, skip
   automatic cleanup for nonblocking findings, and use deterministic status
   comments. Existing registrations preserve their existing behavior until
   explicitly configured. Model-written status is opt-in, with a separate short
   timeout. Blocking findings still require correction and independent review.
5. Runtime: advance immediately after durable progress in watch mode; poll when
   waiting or busy. Distinguish transient model capacity (short cooldown) from
   exhausted subscriptions (existing long cooldown), with bounded retries and
   shared family locks. Avoid redundant comment PATCH requests and decoding
   unrelated repository histories. Reserve review capacity before cloning.
6. Evaluation: preserve existing reliability tests, add regression tests for
   approval identities, mutations, timing/log persistence, defaults/migration,
   progress polling, retry classifications and GitHub no-op writes. Use synthetic
   workflow measurements and clean package installation, without provider calls
   in ordinary tests. Run the full required tests and boundary check.
7. Distribution: publish an unmerged PR in this repository and provide a
   copy-and-paste isolated preview installation pinned to its reviewed commit.
   A preview command suffix lets the maintainer try it on another repository
   without replacing the stable CLI or merging first. Verify the installed
   package works without depending on the development checkout.

## Review and acceptance

Claude Opus 5.5 will independently review this plan before runtime changes and
review the final candidate in a fresh session/checkout. Amend the plan when review
finds a material gap. Publish only after the full checks and independent review
pass; final merging remains human. Do not run live autonomous target-project
work or fabricate issue approval as part of testing.

Acceptance: bot-authored approval cannot authorize work; explicit trusted human
approval can. Blocking/stale/ambiguous evidence never yields readiness. Passing
minor findings stay visible without extra default model rounds. Watch execution
has no unconditional delay between progressing stages. Model/test output survives
timeouts. A new user can install the pinned preview and operate another project
from outside this checkout. CI and ordinary tests make no provider calls.

## Decisions after Claude Opus 5.5 plan review

The independent review requested changes; its material findings are incorporated:

- Approval uses immutable GitHub user IDs resolved when configuring approvers,
  `User` account type, exact template equality, and equal nonempty creation/update
  timestamps. `approve` creates a new comment, never edits another approval.
  Renaming a trusted login preserves identity. Edited/quoted/bot evidence fails.
  Record the approval comment/user/timestamps in the journal and recheck before
  each active stage. On upgrade, bot-approved queued and active work stops until
  a human supplies valid approval; no grandfathering of bot authorization.
- Preview instructions use an isolated venv and separate explicit state home.
  Never register the same target repository in both stable and preview homes.
  Store a version stamp and reject a registry written by a newer version; old
  binaries cannot understand that stamp, so sharing with them is unsupported.
- Every model and test attempt has a unique artifact directory and journal entry.
  Trace reads existing events, including legacy reports. Missing old timing or
  logs is reported as unavailable. Claude moves to documented stream-json output
  with strict final-result parsing and fixture tests; no output reader threads.
- Reject all substitution mechanisms before coordinator Git operations. Review
  checks the entire Git store except index against its baseline, to detect object
  mutation as well as history substitution. Reviewer instructions prohibit
  staging/stashing/committing in the primary review checkout; scratch repositories
  under the working tree remain permitted. This deliberately rejects primary
  object-store writes; their reports/logs remain available as rejected evidence.
- Only explicit model-at-capacity messages get a short cooldown. Ambiguous rate
  limits keep the long quota cooldown. Separate finite capacity/quota counters;
  a shorter failure never reduces an existing longer shared cooldown. Legacy
  projects without a capacity policy retain the old classification/cooldown.
- Immediate watch advancement requires a successful stage transition to another
  active stage. Same-stage, waiting, ready, and error results poll normally. A
  finite cap on consecutive immediate transitions prevents flapping hot loops.
- New keys: minor_cleanup=false, status_mode=template, status_timeout=60,
  capacity_cooldown=60, max_capacity_retries=3. Missing policy keys retain 0.1
  behavior. Existing cleanup rounds finish, frozen revision budgets stay fixed,
  and execution-policy keys do not enter validation configuration fingerprints.
- A held subscription reservation surrounds review clone/patch/call. Other workers
  defer without consuming retries; clone/patch failures do not create cooldowns.
- Work is organized into focused commits with regression coverage. The single
  preview PR will receive a complete exact-commit independent review. Database
  optimization is scoped to Store.repository_runs. Synthetic acceptance compares
  stage delays/model calls/comment writes. Clean package installation is a manual
  verification outside ordinary tests; pinned install instructions live in the
  PR so they can name the reviewed SHA.

Plan review: Claude Code 2.1.292, requested and observed `claude-opus-5-5`,
fresh session, read-only tools. No tests or edits performed by the plan reviewer.
A second plan review was not required by the reviewer after these amendments.
