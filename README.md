# Agent Team

A standalone local coordinator for subscription-backed coding agents. GitHub
holds the issues, PRs, review evidence, and commit status. Your machine runs the
official Codex and Claude Code CLIs. You decide what gets merged.

**Early release:** macOS and Linux, Python 3.11+, GitHub.com, two agent families,
bounded concurrent workers in one shared state directory (serial by default). No Python runtime dependencies, model
API SDKs, server, or repository-installed agent framework.

## What it does

- Alternates implementation between OpenAI/Codex and Anthropic/Claude across
  registered projects; assigns the other family to review in a fresh checkout.
- Claims approved open issues labeled `agent:ready`, validates changes with commands you
  configure, and opens draft PRs. Checks and review must pass before marking ready.
- Records family, CLI version, requested model, observed model when the CLI
  reports it, reviewed commit, findings, and validation results.
- Supports bounded revisions, subscription cooldowns, explicit crash recovery,
  pause/resume, and read-only discovery that opens issues for human triage.
- Never merges PRs, enables auto-merge, closes GitHub issues, or changes branch rules.

## Install

Install Python 3.11+, Git, the [GitHub CLI](https://cli.github.com/),
[Codex CLI](https://developers.openai.com/codex/cli), and
[Claude Code](https://code.claude.com/docs/en/overview).

Install this package with your preferred isolated Python package installer:

```sh
pipx install git+https://github.com/trentoliphant/agent-team.git
```

Or clone this repository and run `python3 -m agent_team` in place. The checkout
has no sibling dependencies and can be moved anywhere. A normal (non-editable)
package installation remains usable after moving or deleting the source checkout.

Sign in through the official CLIs, then verify:

```sh
codex login
claude auth login
gh auth login
agent-team init
agent-team doctor
```

`doctor` does not call a model. `agent-team smoke --agent codex` and
`agent-team smoke --agent claude` make a small real subscription call to test
the adapter, without changing GitHub. Smoke calls share the family lock and respect
existing cooldowns. A smoke quota failure is reported without setting or extending
the production cooldown.

Both agents must report subscription authentication. The coordinator refuses
API-key authentication and excludes API keys and alternate cloud-provider
environment settings from workers. There is no API fallback. Subscription
limits still apply; parallel sessions share your account's capacity. Claude's
`--bare` mode is deliberately avoided because it disables subscription login.
See [authentication and execution boundaries](docs/security.md).

## Use the skill from other repositories

The canonical [Agent Team skill](skills/agent-team/SKILL.md) is maintained here
alongside the CLI. Committing the skill does **not** install it automatically;
installing the Python package does not install it into your chat client either.
It is operating guidance, not a plugin framework or MCP server.

For a personal Codex installation, copy the complete `skills/agent-team`
directory from a reviewed revision into your user skill directory. For example,
run this from an unpacked source release or checkout root:

```sh
mkdir -p "$HOME/.agents/skills"
test ! -e "$HOME/.agents/skills/agent-team" && \
  test ! -L "$HOME/.agents/skills/agent-team" && \
  cp -R skills/agent-team "$HOME/.agents/skills/agent-team"
```

If that destination already exists, inspect it before replacing it. OpenAI's
[Codex skills documentation](https://developers.openai.com/codex/skills)
currently redirects to [Build skills](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).
Its “Where Codex loads local skills” section documents `$HOME/.agents/skills`
as the user location across repositories and explicitly supports symlinked skill
folders (verified September 28, 2026).
The copied directory is self-contained; its broader documentation links are
optional web references. You can remove the source download or checkout after
copying it. The installed CLI and its external prerequisites are still required.
No skill files need to be copied into target repositories.

To update, obtain and review the skill directory from the desired revision, move
your existing personal installation aside (preserving any local edits), and copy
the complete new directory into its place using the commands above. Replace the
directory rather than overlaying files, so removed resources do not linger.
Restart Codex if the change does not appear. Update the installed CLI
separately and keep it compatible with the installed skill's guidance; check
`agent-team --help` and subcommand help before using new examples.

For development, an optional symlink exposes edits from a retained checkout:
after inspecting and moving aside any existing destination, run
`ln -s "$PWD/skills/agent-team" "$HOME/.agents/skills/agent-team"` from that
checkout's root. Only this development option requires retaining the checkout;
if you move it, recreate the link. Review checkout updates before using them.

From a Codex chat opened in another repository, invoke the personal skill, for
example:

```text
$agent-team Identify this repository and show its registered project and status.
$agent-team Prepare registration with this repository's documented validation commands.
$agent-team Discover onboarding gaps for project example and open issues for triage; do not approve or implement them.
```

The installed `agent-team` command works across repositories; its project name
and state directory select the target, not the chat's working directory. Use the
same `AGENT_TEAM_HOME` or global `--home PATH` option across sessions if you use
custom state. Without a package installation, run `python3 -m agent_team` from
the Agent Team checkout, even when operating a different registered project.
The skill follows each target's `AGENTS.md` and references the existing safety
rules. Discovery and issue creation do not authorize implementation: explicit
approval of current issue content is still required, and merging stays human.

## Register a project

Registration stores configuration outside the target repository. It does not
write any project files. Register only repositories you trust to execute locally.

```sh
agent-team project add example your-account/your-repo \
  --test 'python3 -m unittest discover -s tests -v'
agent-team project setup example
agent-team project show example
```

`--test` is repeatable and required. These are operator-approved shell commands,
run from a fresh clone of the candidate commit; agents cannot replace them. Use your
project's existing environment manager in the command when needed. Commands
must be self-contained: do not depend on a development checkout elsewhere.
`project setup` creates only two GitHub labels: `agent:ready` and
`agent:discovered`. The default base branch comes from GitHub; override it with
`--base BRANCH` during registration.

Choose a small GitHub issue with clear acceptance criteria, then approve its
current content. Approval records a SHA-256 fingerprint in a GitHub comment and
adds `agent:ready`. Both the matching approval and label are required. A label
alone cannot authorize a task that someone edits later.

```sh
agent-team approve example 123   # authorize issue #123 as currently written
agent-team run example           # advance one durable stage
agent-team run example --watch   # poll every 30 seconds; Ctrl-C stops
agent-team status
agent-team inspect RUN_ID
```

The sequence is:

```text
ready issue → isolated checkout → implementation → validation → draft PR
                                       ↑                          ↓
                                  bounded revision ← independent review
                                                                  ↓
                                                        GitHub CI → ready for you
```

An implementation failure, ambiguous review, stale commit, or failed CI cannot
produce a successful `agent-team/review` status. Reviews are GitHub comments
with an explicit verdict, not GitHub approval reviews. This works even when
the same GitHub account publishes both authors' PRs; model authorship is recorded
separately from the GitHub account identity.

Use GitHub branch settings to enforce CI and human merge authority. Merely
having a green agent review does not grant permission to merge. See
[GitHub setup](docs/github.md) for dedicated app identities and rulesets.

## Operate the queue

```sh
agent-team project pause example
agent-team project resume example
agent-team resume RUN_ID
agent-team refresh RUN_ID
agent-team close RUN_ID
agent-team project configure example --timeout 1800 --max-revisions 2
agent-team project configure example --quota-cooldown 3600
agent-team project configure example --max-quota-retries 3
```

- Pause takes effect between stages; it does not interrupt an in-flight call.
  You can request pause while a worker is running. Configuration changes require
  all workers to be idle; pause projects first if watch loops are active.
- `resume` retries the recorded stage after you inspect a blocked run. It does not
  apply after the revision limit; see below. Quota
  waits resume automatically after their cooldown, up to three consecutive
  attempts by default. Exhaustion blocks until you explicitly resume.
- `refresh` explicitly adopts the current PR head, merges the current registered
  base into a new checkout, preserves the previous author checkout, and requires
  new tests and review. Conflicts stop without overwriting previous work.
  If a review verdict was saved but the process stopped before the rejection was
  recorded, `refresh` (and `adopt`) records that rejection first and stops. The
  run then revises within the limit or hands off; the rejected commit is never
  reviewed again. `refresh` refuses closed and merged runs, so it cannot reopen
  a run after `close`.
- `close` stops local orchestration only. It preserves work and leaves the
  GitHub issue and PR open for your decision.
- One issue has one run. Closed runs are not silently re-created. Track a new
  attempt in a new linked issue when needed.
- Editing the title/body after approval requires reapproval before assignment.
  Once assigned, the snapshot is immutable: restore it to resume, or close the
  run and create a new linked issue for changed scope.

An unresolved blocked, quota, handoff, or repair run stops new assignments for
that repository, including its other registrations. Ready
and stale PRs do not stop new assignments. A changed PR head/base becomes stale
and requires `refresh`; `resume` cannot reuse its old evidence.
Explicit selection overrides the saved order for one invocation:

```sh
agent-team run example --issue 9
agent-team run example --issue 9 --watch
agent-team queue set example '7, 9, 10, 8, 2'
agent-team queue show example
agent-team queue reorder example '9, 7, 10, 8, 2'
agent-team queue clear example
```

`set` replaces the saved per-project order; `reorder` requires the same entries.
Both reject duplicates and nonpositive numbers. Order persists in the state
registry across restarts. These commands do not change labels or approvals.
`show` reports the effective intake queue, each entry's eligibility reason,
active runs, recovery runs, and pause state. Missing or closed issues are
reported together because neither appears in the open-issue listing. They remain
saved and are skipped, as are unapproved issues, issues without the ready label,
and issues with existing runs (including completed runs).

Without explicit selection, eligible listed issues come first, then unlisted
approved ready issues oldest-first. Clearing the order restores oldest-first.
Explicit selection requires an open issue with current approval and the ready
label. Invalid selections fail without choosing another issue or changing the
saved order. An eligible existing run continues without duplication; completed
runs cannot restart. Another issue's active work or recovery (blocked, quota,
handoff, or repair) prevents targeted execution. Saved ordering applies to new assignments and never
preempts an active run or bypasses recovery.

Targeted watch advances only the selected issue and stops at readiness, pause,
completion, or operator attention. Automatic bounded quota waits keep polling.
Targeted ticks report the selected run's `handoff` or `repair` state without
executing agent work, including after interruption reconciliation.
Untargeted watch can advance other issues in saved order.
The author rotation is global to this state directory and persists across restarts.
Assignments and rotation advance together in a short SQLite transaction.

Enable concurrency while all workers are idle:

```sh
agent-team configure --concurrency 2
agent-team run example --watch
# In another terminal, using the same state directory:
agent-team run another-project --watch
```

The positive concurrency limit defaults to `1`. It limits whole stage ticks,
including validation and GitHub publication. Separate processes run separate
projects; the coordinator does not spawn a worker pool. Watch polls report `busy`
when no slot is available or that repository already has a worker, then retry at
the configured interval. A single tick reports `busy` and returns without work.
Locks are released between ticks and quota waits. Ordinary blocked work in one
repository does not stop unrelated repositories.

Repository identity is the registered GitHub `OWNER/REPO`, compared without case.
Multiple registrations of that identity share a worker lock and issue claims.
Active or interrupted work must continue through its original registration;
another registration reports waiting and cannot adopt its settings. Completed
issue claims remain shared. GitHub renames or transfers are not resolved locally;
keep registrations consistent with the canonical identity.

Subscription calls are serialized per agent family across the state directory.
Codex and Claude can call concurrently. A quota failure starts a shared family
cooldown. Other runs defer without consuming a model call or their quota retry
budget. Actual consecutive quota failures still exhaust the configured bounded
retry budget. Explicit run resume resets that budget but does not bypass a shared
cooldown. Validation, publication, and the other family can continue.

Pause applies only to the named registration and takes effect between ticks;
it does not stop an in-flight stage. Project resume permits scheduling again;
it does not recover blocked work. Configuration changes require all workers to
be idle. Run recovery, decisions, adoption, refresh, approvals, queue edits,
and label setup lock only the affected repository. They remain available while
unrelated workers run, even when all worker slots are occupied. They report lock contention when that repository has a live worker. Discovery takes a repository lock
and a worker slot, so it can overlap unrelated work within the configured limit.
Read-only status and transactional project pause/resume remain available during
execution.

A live repository lock prevents another tick from treating its in-flight marker
as interrupted. After a worker exits, the OS releases its locks. Its next tick
blocks an unfinished stage without repeating agent work. Inspect artifacts and
publication state, then use `agent-team resume RUN_ID` explicitly. A watch process
restart never authorizes repeating an interrupted subscription call. Pending push
SHAs and marked GitHub writes retain their existing reconciliation behavior.

Use **one coordinator state directory on one host per set of repositories**.
These advisory file locks require a local filesystem; sharing state across hosts
or using separate directories for the same repositories provides no coordinated
claims. Stop every worker before backing up, moving, or upgrading shared state.
Do not mix older coordinators with this version against one directory. Companion
repository validation and multi-host coordination are separate features.

Optional model selection is external configuration, not hard-coded in a project:

```sh
agent-team project configure example --codex-model YOUR_MODEL --claude-model YOUR_MODEL
```

Without explicit models, the adapters use the CLIs' defaults. An empty observed
model list means the CLI did not expose the actual model; it is not guessed.

### After the revision limit

Failed validation and rejected reviews share the `--max-revisions` budget. When
it runs out, the run moves to `handoff` and waits. Nothing retries on its own,
and `resume` and `refresh` are refused. Before any GitHub write, the coordinator
saves the rejected review, feedback, revision history, and handoff, with the
review comment and statuses queued. A failed GitHub write cannot block the
handoff; queued writes are retried later. It then posts a handoff
comment on the PR (or on the issue if no PR exists). The comment lists the run,
issue, PR, candidate commit, validation results, and every remaining finding.
It also includes the history and links to evidence. Each finding is labeled
`repeated` (same location and request as an earlier round), `uncertain` (an earlier
round flagged the same file with different wording), `new`, or `first`. If
validation fails after a review rejection, no review has checked that rejection's
findings. The handoff lists them in full with status uncertain, and an extension
passes them to the author along with the validation failure.

```sh
agent-team handoff RUN_ID                       # show the handoff (read-only; --json for the record)
agent-team decide RUN_ID extend --revisions 1   # finite extension, 1-3 more revisions
agent-team decide RUN_ID repair                 # hand off for direct repair (PR branch or local checkout)
agent-team adopt RUN_ID --contributor human     # adopt the repaired commit; repeatable
agent-team decide RUN_ID rescope                # stop; changed scope needs a new linked issue
agent-team decide RUN_ID stop                   # stop; issue, PR, and work are kept
```

`--note TEXT` adds an operator note to the published decision. Each decision is
saved in the run and posted as a PR (or issue) comment. History is never reset.
A rejected commit can never be validated or reviewed again, so every extension
or repair must add a new commit. `extend --revisions N` always allows exactly N
more revisions after the handoff round, even after an adoption. The limit is
stored on the run at the first handoff, so changing the project's
`max_revisions` later does not widen or shrink it. When an extension
is used up, the run returns to `handoff` for a new decision.

For direct repair, push commits to the run's branch, then run `adopt`. List every
contributor with `--contributor openai|anthropic|human`. `Agent-Family` trailers
in the new commits are added too. Adoption is refused while the PR head is still
a rejected commit. It is refused if the head does not build on the last published
candidate, such as a force-push that rewrites history. It is also refused if the reviewer's family contributed,
because no independent agent review is then possible. In that case, review it
yourself, or rescope or stop. An adopted head merges the current base, preserves
the previous checkout, and needs new validation and exact-commit review. It stays
in the same run, PR, and history. A rejection after adoption returns to `handoff`.
If the PR head changes outside the coordinator after an extension or an adoption,
the run goes `stale`. `refresh` then refuses the new head, even without
`Agent-Family` trailers; run `adopt` and declare its contributors. Decisions and
the extension are kept. If that head cannot be adopted, `decide RUN_ID rescope`
or `decide RUN_ID stop` records the decision and closes the run locally.

If validation used up the limit before anything was published, there is no PR
branch. `decide RUN_ID repair` then creates a local repair checkout at the
rejected commit; `agent-team handoff RUN_ID` shows its path. Commit repairs there
on top of that commit, then run `adopt` as above. Uncommitted changes and
rewritten history are refused. Adoption merges the current base into the
repair; on a conflict, the repair checkout is kept so you can merge the base
there and adopt again. The adopted commit is validated before anything
is pushed, then published as a draft PR and independently reviewed as that exact
commit.
`rescope` does not edit the issue. It stops this run; open a linked issue with the
new scope and `approve` it.

## Writing standards

Writing standards shape the GitHub text that agents generate. They cover four
kinds of text: `issue` (discovered issues), `pr` (the author's summary and
limitations in the PR description), `review` (review summary and findings), and
`status` (status comments). Each kind has instructions and an optional word
target. One shared instruction applies to all four.

Precedence, highest first: **project override** (`--project NAME`), then
**personal default** (stored in your state directory), then **built-in
default**. Each value is resolved separately, so a project can override one
review word target and keep your personal review instructions. The built-in
default asks for plain language, concise text, and no repetition.

```sh
agent-team writing show                                   # effective defaults and their sources
agent-team writing set --shared 'Plain language. No filler.'
agent-team writing set --kind review --words 150
agent-team writing set --project example --kind pr \
  --instructions 'Lead with user-visible behavior.' --words 120
agent-team writing set --project example --kind review --words 0   # no target for this project
agent-team writing show --project example --kind pr               # includes the exact prompt text
agent-team writing unset --project example --kind pr --field words
agent-team writing unset --project example --all
```

Setting instructions to `''` or words to `0` clears a lower-precedence value.
`unset` removes a setting so the next level applies again. Like other
configuration changes, `set` and `unset` require all workers to be idle.

The coordinator adds the effective standard to discovery, implementation, and
review prompts, after its fixed rules. The coordinator writes status comments
(issue progress and the PR's ready comment) from plain templates without a model.
Those templates follow the effective `status` word target: when the detailed form
is longer than the target, the coordinator publishes a compact form that leaves
out agent and revision details. Both forms keep the stage, run ID, PR, commit,
validation results, any waiting notice, and the no-merge statement. For example,
`agent-team writing set --kind status --words 20` switches to compact status
comments, and `agent-team writing set --project example --kind status --words 0`
restores detailed ones for one project.

When the effective shared or `status` instructions differ from the built-in
defaults, the run's author agent rewrites each status update to follow them.
For example, `agent-team writing set --project example --kind status
--instructions 'Write in Spanish.'` changes that project's issue progress and
ready comments. The coordinator publishes the agent's wording first, then the
compact template's facts and safeguards unchanged. Each rewrite is an extra
subscription call, made once per distinct update and reused on later ticks. The
agent sees only the template text, never issue text or raw errors. If the call
fails, returns empty text, or is interrupted, the coordinator persists and reuses
its template fallback for that update instead of retrying. Attempts are recorded
before calling the agent. No draft is attempted while waiting for subscription
capacity. Built-in instructions need no model call. Chat-driven
workflows can run `writing show --kind` before drafting GitHub text by hand; see
the [skill](skills/agent-team/SKILL.md).

Standards affect wording only. Word targets are guidance: the coordinator never
truncates agent text, and prompts say not to shorten findings, failures,
evidence, verdicts, limitations, or commit identifiers to fit. A comment longer
than GitHub allows continues in marked follow-up comments that name the
reviewed commit, so every finding is published. Required report
fields, commit SHAs, approval fingerprints, validation results, review
independence, and all authorization and execution checks are enforced in code
and cannot be changed through writing settings.

## Discover work

```sh
agent-team discover example --agent claude \
  --focus 'Onboarding gaps and failure recovery'
```

This performs a read-only source investigation, supplies existing open issues to
the agent, and creates at most three issues labeled `agent:discovered`. Exact
duplicate titles are filtered, while semantic deduplication is advisory. Newly
discovered issues do **not** receive the ready label. Discovery is explicit;
schedule it with your operating system if desired. It does not run interactive
product usability exercises in this release.

## Storage and portability

State defaults to `$XDG_DATA_HOME/agent-team`, or `~/.local/share/agent-team`.
Override with `AGENT_TEAM_HOME` or the global `--home PATH` option.

```text
state.sqlite3          projects, runs, rotation, transition journal
runs/<id>/author/     isolated Git clone
runs/<id>/validation-*/ fresh clones of the exact candidate for test execution
runs/<id>/review-*/   separate candidate review clones
runs/<id>/artifacts/  prompts, structured reports, local test and worker logs
discovery/            read-only investigation clones and reports
```

No credentials are stored in this registry. The official CLIs manage their own
logins. Stop the coordinator before backing up or moving its entire state
directory (including SQLite sidecar files). Run paths are derived from the state
directory, so moving it does not require database edits. Moving the source
repository is independent of moving state. Artifacts have no automatic retention
policy yet; inspect disk usage before long unattended operation.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_boundary.py
```

Tests use local Git repositories and fake agent/GitHub responses. They do not
consume subscriptions, require credentials, or call the network. CI runs these
checks on Linux and macOS with Python 3.11 and 3.14.

See [architecture and limitations](docs/architecture.md),
[security](docs/security.md), and [contributing](CONTRIBUTING.md).

## Selected stop boundaries (initial support)

`agent-team run example --issue 123 --stop-after validate --watch` implements
an approved issue and validates it without pushing or creating a PR. The boundary
is saved atomically with assignment. Later ticks, watch restarts, and `resume`
cannot expand it. Successor stages are checked before execution, crash recovery,
and explicit resume. Pending CI remains in `ci`; successful CI retains `ready`
and its normal reconciliation. Stopped runs with PRs also reconcile head/base
changes and closure. A stopped run requires operator attention and blocks intake until explicit continuation or closure.
The result certifies only the performed stages. Validation or review rejection
records the existing revision history and budget, then stops before applying fixes.

| Command selection | Input | Effects | Prerequisites |
| --- | --- | --- | --- |
| `--stop-after implement` | Approved ready issue | Isolated local edits, issue status comments | Current approval fingerprint |
| `--stop-after validate` | Approved ready issue | Above, local candidate commit and configured tests | Current approval fingerprint |
| `--stop-after publish` | Approved ready issue | Above, branch push and draft PR | Successful validation |
| `--stop-after review` | Approved ready issue | Above, independent review comments and statuses | Exact candidate, other author family |
| `--stop-after ci` | Approved ready issue | Full pipeline through PR readiness | Validation, independent review, successful CI |

These selections all start with preparation and implementation. They retain the
existing issue-status writes. They do not separate individual effect permissions.
Independent entry from branches, commits, scoped tasks without issues, or PRs
is not implemented yet. Existing-PR entry belongs to companion #10. Tracked-run
continuation supports the operations below.
Do not create synthetic issues or rerun implementation to substitute for these
unsupported operations. Discovery retains its existing separate command.

### Explicit continuation of tracked work

`continue` selects a contiguous sequence starting at the stopped run's recorded
`next_stage`. It preserves the candidate, rejection history, round, and revision
limit. Repeating the same command before execution does not add another handoff.
It does not execute a stage; use `run --issue` to advance the saved selection.

```sh
# Implementation without publication, then validation only:
agent-team run example --issue 123 --stop-after implement --watch
agent-team continue RUN_ID --operations validate
agent-team run example --issue 123 --watch
# Publish the tracked, validated candidate without another implementation:
agent-team continue RUN_ID --operations publish
agent-team run example --issue 123 --watch
# Review only, followed later by CI/readiness:
agent-team continue RUN_ID --operations review
agent-team run example --issue 123 --watch
agent-team continue RUN_ID --operations ci
agent-team run example --issue 123 --watch
# After a rejection, explicitly authorize revision through review:
agent-team continue RUN_ID --operations implement validate publish review
agent-team run example --issue 123 --watch
```

| Continuation | Input | Effects | Prerequisites |
| --- | --- | --- | --- |
| `validate` | Stopped tracked run | Candidate commit, configured tests, issue status | Recorded next stage; unchanged approved scope |
| `publish` | Stopped tracked run | Push, draft PR creation/update, statuses | Compatible successful validation |
| `review` | Stopped tracked run with PR | Subscription review, comments/statuses | Exact candidate and independent family |
| `implement validate publish review` | Rejected tracked run | Local revision through review; push and GitHub writes | Remaining revision budget; recorded implementation successor |
| `ci` | Reviewed tracked run | CI reads, status/comment writes, PR readiness | Exact passing review and validation |

Continuation compares local HEAD, committed tree (including tracked dependency
pins), dirty state, issue scope, and validation/model configuration. Drift clears
validation/review eligibility and records the old and new context. Changed remote
base refuses continuation and requires explicit integration; unpublished base
integration is not provided by this command. PR reconciliation still checks head,
base, and closure. Failed evidence and revision budgets remain durable.

These commands retain the full workflow's effect authorization. Separate grants
for local edits, push, GitHub content, and readiness are not implemented. Inputs
outside tracked approved-issue runs remain unsupported; this change does not
complete issue #9 or companion #10.
