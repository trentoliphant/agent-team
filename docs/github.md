# GitHub setup

Run `gh auth login` or supply a short-lived GitHub App installation token as
`GH_TOKEN` to the coordinator. Agent Team targets GitHub.com. No token is written
to the project registry or passed to model workers.

For a dedicated app, grant only the repositories it will operate on, with:

- Metadata: read
- Contents: read/write (topic branches)
- Issues: read/write (queue, labels, status comments)
- Pull requests: read/write (draft PRs and review comments)
- Commit statuses: read/write (`agent-team/review`)
- Checks: read (CI results)

The coordinator does not create apps, mint or refresh installation tokens, or
grant repository permissions. Supply renewed tokens from your own trusted launcher.
Do not use a personal subscription login as a shared account for other people.

## Labels and issue authorization

`agent-team project setup PROJECT` creates the ready and discovery labels.
`agent-team approve PROJECT NUMBER` posts an approval of the current issue
title/body fingerprint and applies the ready label. Both are required for intake;
approval must be written by the coordinator's current GitHub identity. Approve
only issues whose scope and acceptance criteria are clear enough for autonomous
work. Later edits invalidate approval; edits during a run stop that run.
Removing the label stops further work on the next active stage. Discovered issues
remain unready until triaged. One registered project corresponds to one repository.

Status is maintained in one marked comment per issue. Review evidence and the
ready-for-maintainer summary are marked comments on the PR. A failed validation
gets its own comment naming the failing tests; raw output stays local. A run that
reaches its revision limit keeps one handoff comment, updated in place at each
handoff and operator decision. When the PR is merged or closed, an outcome comment
records the revisions, rejections, findings, and decisions the run took. Review,
validation, handoff, and outcome comments end with a collapsed machine-readable
record. Revision commits name the rejection they answer in their subject.
Duplicate suppression
only trusts comments written by the coordinator's current GitHub identity. Avoid
switching identities mid-run if you want to retain a single status comment.

## Human merge authority

Configure the protected default branch using GitHub repository settings:

1. Require a PR and prevent force pushes and branch deletion.
2. Require your existing deterministic CI checks and `agent-team/review` for
   agent-managed PRs. The status must have run at least once before it can be
   selected. Requiring it branch-wide also affects human PRs; arrange an explicit
   trusted human-review path rather than leaving those PRs permanently blocked.
3. Where supported, bind the review status to the dedicated app as expected source.
4. Use a separate update-restriction ruleset that permits only your maintainer
   identity/team to update the protected branch. The automation app gets no bypass.
   Keep CI/PR rules in a separate ruleset so this exception does not bypass them.
5. Disable auto-merge if you want every merge to be your direct action. Decide
   separately whether you want native human approval requirements.

GitHub approval reviews cannot be used to approve your own PRs. Agent Team posts
independent model verdicts as comments and a commit status, so using your personal
GitHub identity initially does not create a self-approval deadlock. A distinct app
identity makes authorship and permission enforcement clearer.

Review completion and merge acceptance are separate. The coordinator can mark a
PR ready; it never merges. Exact permissions and rule availability depend on the
repository owner and GitHub plan. See
[GitHub rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

## CI events from automation

Use your app token or authenticated local `gh` identity for publication. GitHub
may suppress downstream workflow events when writes use an Actions job's built-in
`GITHUB_TOKEN`. This coordinator is designed to run locally with CLI subscriptions;
it does not use that workflow token. GitHub Actions remains suitable for ordinary
model-free tests in the target repositories.
