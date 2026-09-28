# Agent Team

A standalone local coordinator for subscription-backed coding agents. GitHub
holds the issues, PRs, review evidence, and commit status. Your machine runs the
official Codex and Claude Code CLIs. You decide what gets merged.

**Early release:** macOS and Linux, Python 3.11+, GitHub.com, two agent families,
one worker at a time per state directory. No Python runtime dependencies, model
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
the adapter, without changing GitHub.

Both agents must report subscription authentication. The coordinator refuses
API-key authentication and excludes API keys and alternate cloud-provider
environment settings from workers. There is no API fallback. Subscription
limits still apply; parallel sessions share your account's capacity. Claude's
`--bare` mode is deliberately avoided because it disables subscription login.
See [authentication and execution boundaries](docs/security.md).

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
  an idle worker lock; pause first if a watch loop is active.
- `resume` retries the recorded stage after you inspect a blocked run. Quota
  waits resume automatically after their cooldown, up to three consecutive
  attempts by default. Exhaustion blocks until you explicitly resume.
- `refresh` explicitly adopts the current PR head, merges the current registered
  base into a new checkout, preserves the previous author checkout, and requires
  new tests and review. Conflicts stop without overwriting previous work.
- `close` stops local orchestration only. It preserves work and leaves the
  GitHub issue and PR open for your decision.
- One issue has one run. Closed runs are not silently re-created. Track a new
  attempt in a new linked issue when needed.
- Editing the title/body after approval requires reapproval before assignment.
  Once assigned, the snapshot is immutable: restore it to resume, or close the
  run and create a new linked issue for changed scope.

An unresolved blocked/quota run stops new assignments for that project. Ready
and stale PRs do not stop new assignments. A changed PR head/base becomes stale
and requires `refresh`; `resume` cannot reuse its old evidence.
Selection is oldest eligible issue first.
The author rotation is global to this state directory and persists across restarts.
Two processes sharing the same directory cannot execute stages concurrently.
Use **one coordinator state directory per set of repositories**; separate hosts
do not share a distributed claim lock.

Optional model selection is external configuration, not hard-coded in a project:

```sh
agent-team project configure example --codex-model YOUR_MODEL --claude-model YOUR_MODEL
```

Without explicit models, the adapters use the CLIs' defaults. An empty observed
model list means the CLI did not expose the actual model; it is not guessed.

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
