# Try the 0.2 preview on another project

The preview does not need to be merged. Its PR description supplies the full
reviewed commit SHA and a pinned installation command. Python 3.11+, Git, gh,
Codex and Claude Code are the prerequisites. The preview installer creates an
isolated venv, installs that exact commit from public GitHub, and writes
`agent-team-preview` in your user executable directory. It installs no skill or
provider credentials. It makes no model calls and no GitHub writes.

From a new directory, substitute the full reviewed SHA supplied in the PR. The
same SHA selects the installer and the installed package:

```sh
git clone https://github.com/trentoliphant/agent-team.git agent-team-preview-source
cd agent-team-preview-source
git checkout --detach FULL_REVIEWED_COMMIT_SHA
python3 scripts/install_preview.py --ref FULL_REVIEWED_COMMIT_SHA
```

After installation you can remove that source checkout; the installed command
works from any directory. If you already have this preview checkout, use:

```sh
python3 scripts/install_preview.py --ref FULL_REVIEWED_COMMIT_SHA
agent-team-preview --version
agent-team-preview doctor
```

If your shell cannot find the command, use `"$HOME/.local/bin/agent-team-preview"`.
The wrapper always selects its separate preview state directory. Your stable
command and registry remain available. Register a target repository in only
one of the stable and preview state directories: separate registries cannot
coordinate claims or locks with each other. Do not point an older CLI at preview
state. Newer registry stamps are rejected by this version, but old releases do
not understand the stamp. Updating this preview uses the same installer with a
new reviewed SHA. Removing it means removing the wrapper and venv; retain state
and artifacts if you want the history. The default uninstall paths are
`$HOME/.local/bin/agent-team-preview` and
`$HOME/.local/share/agent-team-preview/venv`; retain the sibling `state` directory.

Choose a repository you own and trust to execute locally, with a small open issue
and clear acceptance criteria. Use its documented validation commands:

```sh
agent-team-preview project add trial OWNER/REPO \
  --approver YOUR_HUMAN_GITHUB_LOGIN \
  --claude-model claude-opus-5-5 \
  --test 'YOUR_PROJECT_TEST_COMMAND'
agent-team-preview project setup trial
agent-team-preview project show trial
```

The working directory does not select the project. These commands work from
any directory, and registration writes no files into the target project.
`--test` is repeatable and trusted shell code. Agents cannot replace it. If the
repository needs a build step or lint, add the documented commands as further
`--test` options. No existing project environment is copied into the run.

Approve the actual issue as a human, then let it proceed:

```sh
agent-team-preview approve trial ISSUE_NUMBER
agent-team-preview run trial --issue ISSUE_NUMBER --watch
agent-team-preview status --project trial
agent-team-preview trace RUN_ID
```

Under a dedicated bot coordinator identity, `approve` intentionally fails. Use
`agent-team-preview approval-text trial ISSUE_NUMBER`, copy its entire output
into a **new** GitHub issue comment signed in as a configured human approver,
and add `agent:ready` on GitHub. CRLF line endings and trailing whitespace are normalized; all substantive text
must exactly match the template. The comment must remain unedited. Edited 0.1
approval comments (including those re-approved in place) need a new human approval
after upgrading. To renew an
approval, post a new comment. A quoted template or a bot-authored/edited comment
cannot authorize execution. Active work rechecks approval before each stage;
deleting it or removing its approver blocks further work. Removing the ready
label also stops authorization. Human credentials shared with an agent cannot
prove the operator is human; use separate identities and branch rules.

Watch proceeds immediately between successful active stages and polls while
waiting for CI or subscription capacity. Ctrl-C stops it; inspect artifacts
before `resume` when it interrupts a stage. The ready PR has independent review
of the exact validated commit and any remaining minor findings. Inspect it and
merge yourself using GitHub; the coordinator has no merge operation.

New registrations use four correction rounds, deterministic status prose and
no automatic minor cleanup. The initial attempt plus four corrections means
at most five author calls and five review calls before a handoff, excluding
bounded capacity retries and optional explicit extensions. Each command/model
call has the configured timeout. Existing registrations preserve their stored
revision limit and legacy cleanup/prose behavior until configured. To adopt the
preview policy for an existing project, explicitly use:

```sh
agent-team-preview project configure trial --max-revisions 4 --no-minor-cleanup \
  --status-mode template --status-timeout 60 \
  --capacity-cooldown 60 --max-capacity-retries 3
```

`--status-mode model` allows custom status instructions to request a model draft;
its separate short timeout and one-attempt fallback limit optional overhead.
Only explicit model-at-capacity errors use the short retry policy; ambiguous
rate/usage limits keep the long subscription cooldown. Both have finite counters,
and all workers continue sharing per-family locks. No automatic model/API switch.

`trace RUN_ID --json` exposes the durable reports and journal for further analysis.
The human form lists approval, model rounds, findings and responses, configured
validation (including omitted commands), attempts, stage time and artifact paths. Author
rounds distinguish their input commit from the subsequent candidate. Missing
responses remain recorded after validation and readiness; validation-only
feedback does not create a missing review response.
It is a local snapshot; it does not verify current remote state or certify that
an author's response resolved a finding. Missing legacy history is marked as
unrecorded. Unique per-attempt stdout/stderr logs retain partial output on timeouts
and interrupts. CLI token usage is diagnostic and is not subscription billing.
