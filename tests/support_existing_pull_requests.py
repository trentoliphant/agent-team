"""Offline fixture for existing-PR workflow tests; it defines no tests."""
from unittest.mock import patch

from agent_team.coordinator import LOCAL_CHANGE
from agent_team.github import GitHub
from agent_team.process import git, TeamError

# Module imports, so discovery collects neither the fixture class nor WorkflowTests here.
from tests import support_pull_requests as support, test_coordinator

ALL = ["edit", "push", "github"]
READY = ["github", "readiness"]
FIX = ["Append a closing line to feature.txt"]
RECHECK = ["validate", "review"]
REVISION = ["revision", "validate"]
FOLLOWUP = [*REVISION, "publish", "review"]
CI_FAILED = "GitHub CI checks for the current candidate did not pass (state: {})."
HISTORICAL_CI = "GitHub CI checks were not checked for the current candidate and base; earlier observations are historical."
DELIBERATE = "Deliberate adoption of changed PR head or base"
DRIFT = "Candidate or configuration changed"
PUSHED = "Revision pushed; independent review pending"
STALE_READY = "Agent Team marked the PR ready for {}; that readiness is not current."
NO_BUDGET = "no revision budget left"
INTERRUPTIONS = ("journal", "rename-1", "rename-2", "final-save")
MOVES = ("head", "base", "identity", "configuration")
REASONS = {"configuration": "Validation configuration changed", "pins": "Companion pins changed",
           "local": LOCAL_CHANGE, "dirty": LOCAL_CHANGE, "head": "PR head moved", "base": "PR base changed"}


def unavailable(*_, **__):
    raise TeamError("GitHub unavailable")


class FeatureGitHub(support.PullGitHub):
    def pr(self, repo, number):
        return GitHub.pr(self, repo, number) if number in self.pulls else super().pr(repo, number)

    def push_access(self, project, pr):
        return GitHub.push_access(self, project, pr)


class ExistingPullRequestFixture(support.PullRequestFixture):
    provider = FeatureGitHub
    command = test_coordinator.WorkflowTests.command

    def pinned(self, kind):
        return patch.object(self.team, "pins_changed", return_value=kind == "pins")

    def invalidation(self, run):
        return run["evidence_invalidations"][-1]

    def adopt_pr(self, mode="review", number="7", contributors=("human",), **options):
        return self.team.adopt_pr("demo", number, mode, list(contributors), **options)

    def editing(self, mode="revise", number="7", **options):
        return self.adopt_pr(mode, number, grants=["edit"], **options)

    def editing_findings(self, **options):
        return self.editing("findings", findings=["Fix it"], **options)

    def select(self, run, operations, grants=(), **options):
        return self.team.select("demo", list(operations), list(grants), run_id=run["id"], **options)

    def revalidate(self, run, **options):
        return self.ticks(self.select(run, RECHECK, **options), 2)

    def update(self, run):
        return self.team.update_pr(run["id"], ["human"])

    def adopt_repair(self, run, contributors=("human",)):
        return self.team.adopt(run["id"], list(contributors))

    def report(self, run):
        return self.team.pr_report(run["id"])

    def writable(self, mode="revise", grants=ALL, **options):
        self.github.permissions["example/demo"] = True
        return self.adopt_pr(mode, grants=grants, **options)

    def reviewed(self, number="7", count=3, contributors=("human",), **options):
        return self.ticks(self.adopt_pr("review", number, contributors, **options), count)

    def under_review(self, reject=False, **options):
        head = self.open_pr(reject=reject)
        return head, self.reviewed(count=2, **options)

    def stopped_review(self, adopt=None, **options):
        head = self.open_pr()
        run = self.ticks((adopt or self.adopt_pr)(**options), 3)
        self.assert_reviewed(run, head)
        return head, run

    def readiness_run(self):
        head, run = self.stopped_review(grants=["github"])
        self.assertEqual(self.report(run)["ci_checks"], "not checked")
        return head, self.select(run, ["ci"], READY)

    def at_publish(self):
        self.open_pr(reject=1)
        run = self.ticks(self.writable(), 5)
        self.assertEqual(run["stage"], "publish")
        return run

    def repair_handoff(self, **options):
        self.configure(max_revisions=0)
        self.open_pr(reject=True)
        run = self.reviewed(**options)
        self.assertEqual(run["stage"], "handoff")
        self.assertIn("feature.txt:1", run["handoffs"][0]["text"])
        run = self.team.decide(run["id"], "repair")
        self.assertEqual(run["stage"], "repair")
        return run

    def interrupted_swap(self, updating, point):
        if updating:
            head, run = self.stopped_review()
            external = self.push_external()
            self.assertEqual(self.ticks(run)["stage"], "stale")
            recover = lambda: self.update(run)
        else:
            run = self.repair_handoff()
            head, external, recover = run["sha"], self.push_external(message="Repair"), lambda: self.adopt_repair(run)
        self.interrupt(point, recover)
        return run, head, external, recover

    def marker(self, run, head, round_=0):
        return 7, f"{run['id']}-review-{round_}-{head}"

    def lost_push(self, cwd, *args):
        result = self.local_git(cwd, *args)
        if "push" in args:
            self.assertFalse(any(str(a).startswith("+") or a == "--force" for a in args))
            self.assertIn(f"{self.workspace_head(self.store.runs()[0])}:refs/heads/feature", args)
            raise TeamError("Simulated lost push response")
        return result

    def assert_reviewed(self, run, sha, **fields):
        self.assert_fields(run, stage="stopped", reviewed_sha=sha, **fields)

    def assert_awaiting(self, run, next_stage="validate", **fields):
        self.assert_fields(run, stage="stopped", next_stage=next_stage, **fields)

    def assert_revised(self, run, head, **fields):
        self.assert_reviewed(run, run["sha"], **fields)
        self.assertEqual(self.agents.calls,
                         [(run["reviewer"], "review"), (run["author"], "implement"), (run["reviewer"], "review")])
        self.assertEqual((self.remote_head(), git(self.remote, "rev-parse", f"{run['sha']}^")), (run["sha"], head))

    def assert_stop_boundary(self, run, head):
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], self.roles()), ("stopped", ["review"]))
        self.assert_untouched(head)
        return run

    def assert_local_pending(self, run):
        self.assertTrue(run["pending_contribution"])
        self.assertIn(LOCAL_CHANGE, self.invalidation(run)["reason"])

    def assert_not_independent(self, run, reason=""):
        self.assertFalse(run["independence"]["established"])
        self.assertIn(reason, run["independence"]["reason"])

    def assert_historical(self, report):
        self.assertFalse(report["current_evidence"] or report["independent_review_success"]
                         or report["validation"]["passed_for_candidate"])

    def assert_retired(self, report, head, base, reason, verdict="pass", **fields):
        self.assert_historical(report)
        historical = report["historical_evidence"][-1]
        self.assert_fields(historical, head=head, base=base, verdict=verdict, **fields)
        self.assertIn(reason, historical["reason"])
        return historical

    def assert_withheld(self, run, head, unresolved=None):
        self.assertEqual((run["stage"], run.get("reviewed_sha"), run["review_withheld"]["sha"]), ("stopped", None, head))
        report = self.report(run)
        self.assertFalse(report["independent_review_success"])
        if unresolved is not None:
            self.assertEqual(report["authorship"]["unresolved_trailers"], unresolved)

    def refuses_closed(self, number=7, check=lambda: None):
        for field, value, message in (("state", "closed", "never reopens"), ("merged", True, "merged")):
            self.pull(number)[field] = value
            check()
            self.refuses(message, self.adopt_pr, number=str(number))
        self.assertEqual(self.pull(number)["state"], "closed")

    def refuses_revision(self, message, run):
        self.refuses(message, self.select, run, REVISION, ["edit"])
