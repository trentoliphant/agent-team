"""Existing-PR adoption, offline."""
from contextlib import nullcontext, suppress
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_team import coordinator
from agent_team.cli import parser
from agent_team.coordinator import FAMILIES, LOCAL_CHANGE, pull_number, review_comment
from agent_team.github import GitHub
from agent_team.patches import COMPACT_NOTICE
from agent_team.process import git, TeamError

# Module imports avoid rerunning other tests.
from tests import support_pull_requests as support, test_coordinator
from tests.support_pull_requests import COMMIT, FORK, Interrupted, scenarios, trailed

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


class PullRequestTests(support.PullRequestFixture):
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

    @scenarios((False, []), (True, ["github"]), (True, []))
    def test_review_only_reports_findings_and_stops(self, rejected, grants):
        head = self.open_pr(user="claude-bot")
        self.agents.reject, self.agents.summary = rejected, "Feature text is wrong"
        run = self.adopt_pr(number="https://github.com/example/demo/pull/7" if rejected else "7", grants=grants)
        self.assert_fields(run, pr=7, sha=head, base_sha=self.remote_head("main"), issue=None, contributors=["human"])
        self.assert_fields(run["adopted_pr"], head_repo="example/demo", head_ref="feature", base_ref="main",
                           state="open", draft=True, mode="review")
        self.assertEqual(run["adopted_pr"]["github_identities"]["pr_author"], "claude-bot")
        self.assertTrue(run["independence"]["established"])
        run = self.ticks(run, 3)
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
        if grants:
            self.assert_contains(self.github.comments[self.marker(run, head)], "changes requested",
                                 "GitHub CI checks: not checked by this review.", "not a readiness verdict")
            self.assertIn((head, "failure"), self.github.statuses)
        else:
            self.assert_quiet()
        report = self.report(self.assert_stop_boundary(run, head))
        review = report["review"]
        self.assert_review(review, head, True, "changes_requested" if rejected else "pass", agent=run["reviewer"])
        self.assert_fields(report, ci_checks="not checked", local_handoff=None)
        self.assertIsNone(report["roles"]["reviser"])
        self.assertIn("GitHub CI checks were not checked.", report["limitations"])
        self.assert_limited(report, "Readiness was not assessed")
        if not rejected:
            self.assert_reviewed(run, head)
            self.assert_fields(report, currency={"verified": True, "reason": None})
            self.assertTrue(report["independent_review_success"] and report["validation"]["passed_for_candidate"])
            return
        self.assert_awaiting(run, "implement", review_record=None)
        self.assertIn(head, run["rejected_shas"])
        self.assert_fields(review, summary="Feature text is wrong", candidate_verdict="changes_requested",
                           validation_failed=False)
        self.assertEqual(review["findings"][0]["location"], "feature.txt:1")

    def test_adoption_refuses_invalid_requests(self):
        self.open_pr()
        for mode, grants, message in (
                ("review", ["edit"], "Review-only never edits"), ("review", ["push"], "Review-only never edits"),
                ("revise", [], "requires --grant edit"), ("review", ["readiness"], "never changes PR readiness"),
                ("findings", ["edit"], "--finding")):
            with self.subTest(mode=mode, grants=grants):
                self.refuses(message, self.adopt_pr, mode, grants=grants)
        self.refuses("--finding", self.adopt_pr, findings=["Fix it"])
        self.refuses("Declare every contributor", self.adopt_pr, contributors=())
        self.refuses("not registered repository", self.adopt_pr, number="https://github.com/other/demo/pull/7")
        self.assertEqual((pull_number(self.project, "https://github.com/Example/Demo/pull/7/files"), self.store.runs(),
                          self.agents.calls), (7, [], []))

    def test_revise_fast_forwards_existing_branch(self):
        head = self.open_pr(reject=1)
        run = self.writable()
        self.assert_fields(run, operations=RECHECK, pr_followup=FOLLOWUP)
        run = self.ticks(run, 7)
        self.assert_revised(run, head)
        self.assert_fields(run["adopted_pr"], pushed=[run["sha"]])
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual((self.github.creates, self.pull()["body"], self.pull()["draft"]), (0, "Human description", True))
        self.assert_status(run["sha"], "pending", PUSHED)
        self.assert_no_success()
        self.assertIn(self.marker(run, run["sha"], round_=1), self.github.comments)
        report = self.report(run)
        self.assert_review(report["review"], run["sha"], True)
        self.assert_review(report["review_history"][0], head, False, "changes_requested", run["base_sha"], run["reviewer"])

    @scenarios(True, False, None)
    def test_fork_push_or_local_handoff(self, editable):
        head = self.open_pr(8, "fork-feature", head_repo=FORK, maintainer_can_modify=editable)
        self.github.permissions["example/demo"] = editable
        if editable is None:
            self.github.repo = unavailable
        plan = self.adopt_pr("revise", "8", grants=ALL, plan_only=True)
        self.assertEqual((plan["push"]["allowed"], self.store.runs()), (bool(editable), []))
        self.assertIn("maintainer edits" if editable else FORK, plan["push"]["reason"])
        if editable:
            self.assertIn("publish", plan["revision_operations"])
            self.refuses("github", self.adopt_pr, "revise", "8", grants=["edit", "push"])
            return
        self.agents.reject = 1
        run = self.adopt_pr("revise", "8", grants=ALL)
        self.assertEqual(run["adopted_pr"]["head_repo"], FORK)
        self.assertNotIn("publish", run["pr_followup"])
        run = self.ticks(run, 6)
        self.assert_reviewed(run, run["sha"], validated_sha=run["sha"])
        self.assert_untouched(head, 8, "fork-feature")
        handoff = self.report(run)["local_handoff"]
        self.assert_fields(handoff, commit=run["sha"], builds_on=head, replacement_pr="not created")
        self.assertIn(FORK, handoff["reason"])
        self.assertIn("not be verified" if editable is None else "", handoff["reason"])
        self.assertIn("fixed", Path(handoff["patch"]).read_text())
        self.refuses("published PR head", self.select, run, ["ci"], READY)
        self.store.save(run, stage="ci", grants=READY, operations=["ci"], stop_after="ci")
        self.assert_error(self.ticks(run), "blocked", "published PR head")
        self.assert_no_success()
        self.assertTrue(self.pull(8)["draft"])

    @scenarios((None, ["human", "unknown"], "unknown contributors", ["human", "unknown"], []),
               ("anthropic", ["openai"], "both model families", ["anthropic", "openai"], []),
               ("unknown", ["human"], "unknown or unsupported model families", ["human"], ["unknown"]),
               ("gemini", ["human"], "unknown or unsupported model families", ["human"], ["gemini"]))
    def test_unresolved_authorship_withholds_success(self, trailer, declared, refusal, contributors, unresolved):
        head = self.open_pr(families=[trailer], user="openai-codex")
        self.refuses(refusal, self.editing, contributors=declared)
        run = self.adopt_pr(contributors=declared, grants=["github"])
        self.assertEqual(run["contributors"], contributors)
        self.assert_fields(run["adopted_pr"], unresolved_trailers=unresolved,
                           trailer_families=["anthropic"] if trailer == "anthropic" else [])
        self.assert_not_independent(run, refusal)
        run = self.ticks(run, 3)
        self.assert_withheld(run, head, unresolved)
        self.assert_contains(self.github.comments[self.marker(run, head)], "Review (independence not established)",
                             f"Independent-review success withheld: {run['independence']['reason']}")
        self.assert_no_success()
        self.refuses("exact-commit independent review", self.select, run, ["ci"], ["readiness"])

    def test_single_family_independent_roles(self):
        self.open_pr(families=["openai"])
        self.refuses("cannot review independently", self.adopt_pr, reviewer="codex")
        run = self.adopt_pr()
        self.assert_fields(run, author="codex", reviewer="claude", contributors=["human", "openai"])
        self.store.save(run, stage="closed")
        self.open_pr(9, "branch-9", families=["openai", "gemini"])
        run = self.adopt_pr(number="9")
        self.assert_fields(run, author="codex", reviewer="claude")
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["openai"])
        self.assert_not_independent(run)

    def test_duplicate_adoption_refused(self):
        self.open_pr()
        run = self.adopt_pr()
        self.refuses(f"already tracked by run {run['id']}", self.editing)
        self.store.save(run, stage="closed")
        self.refuses("already has a run", self.adopt_pr)
        owned = self.store.create(self.project, self.github.items[0])
        self.store.save(owned, pr=9, stage="stopped")
        self.refuses(f"already tracked by run {owned['id']}", self.adopt_pr, number="9")
        self.assertEqual(len(self.store.runs()), 2)

    def test_readoption_keeps_exhausted_budget(self):
        self.configure(max_revisions=1)
        self.open_pr(reject=2)
        run = self.until_handoff(self.editing())
        self.assert_fields(run, stage="handoff", round=1)
        self.assertEqual(self.team.decide(run["id"], "stop")["stage"], "closed")
        external = self.push_external()
        self.configure(max_revisions=5)
        calls = list(self.agents.calls)
        self.refuses(f"reached its revision limit in run {run['id']}", self.editing)
        self.refuses("reached its revision limit", self.editing_findings)
        self.assertEqual(len(self.store.runs()), 1)
        review = self.adopt_pr()
        self.assert_fields(review, sha=external, round=1, revision_limit=1)
        self.assert_fields(review["prior_runs"][0], id=run["id"], handoffs=1, decisions=["stop"])
        self.assertEqual((set(review["rejected_shas"]), self.agents.calls), (set(run["rejected_shas"]), calls))

    def test_readoption_continues_budget(self):
        self.configure(max_revisions=2)
        self.open_pr(reject=True)
        run = self.reviewed()
        self.assert_fields(run, stage="stopped", round=1)
        self.store.save(run, stage="closed")
        self.push_external()
        self.assertEqual(self.editing(plan_only=True)["revision_budget"],
                         {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        self.assertEqual(self.editing_findings(plan_only=True)["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        self.refuses(NO_BUDGET, self.editing_findings)
        self.assert_fields(self.editing(), round=2, revision_limit=2)

    def test_closed_or_other_base_before_mutation(self):
        self.open_pr()
        self.refuses_closed()
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(8, "release-fix", base="release")
        self.refuses("never retargets", self.editing, number="8")
        run = self.adopt_pr(number="8")
        self.assert_reviewed(self.ticks(run, 3), head)
        self.assertEqual((run["adopted_pr"]["base_ref"], self.pull(8)["base"]), ("release", "release"))

    def test_closed_pr_with_deleted_base(self):
        git(self.remote, "branch", "release", "main")
        self.open_pr(8, "release-fix", base="release")
        snapshot = self.pull(8)["frozen_base"]
        git(self.remote, "branch", "-D", "release")
        with self.assertRaises(TeamError):
            self.github.pr(None, 8)
        self.refuses_closed(8, lambda: self.assertEqual(self.github.pr(None, 8)["base"]["sha"], snapshot))

    @scenarios((7, "feature", "main", ["human", "unknown"], "unknown contributors"),
               (8, "mixed", "main", ["openai", "anthropic"], "both model families"),
               (9, "release-fix", "release", ["human"], "never retargets"))
    def test_refused_revision_never_edits(self, number, branch, base, contributors, message):
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(number, branch, base=base, reject=True)
        run = self.reviewed(str(number), contributors=contributors)
        self.assert_awaiting(run, "implement")
        calls = list(self.agents.calls)
        self.refuses_revision(message, run)
        run = self.reload(run)
        self.assertEqual(run["stage"], "stopped")
        self.store.save(run, stage="revision", grants=["edit"], operations=REVISION, stop_after="validate")
        try:
            self.ticks(run)
        except TeamError as error:
            self.assertRegex(str(error), message)
        run = self.reload(run)
        self.assertNotIn(run["stage"], {"validate", "review"})
        self.assertEqual(self.agents.calls, calls)
        self.assertNotIn("implement", self.roles())
        cwd = self.store.workspace(run)
        self.assertEqual((self.head_of(cwd), git(cwd, "status", "--porcelain"), self.remote_head(branch)),
                         (head, "", head))
        self.change("dirty", run)
        self.store.save(run, stage="validate", evidence_context=self.team.evidence_context(self.store.project("demo"), run))
        self.assertRegex(self.tick_raising(run)["error"], message)
        self.assertEqual((self.head_of(cwd), self.remote_head(branch)), (head, head))

    @scenarios("success", "pending")
    def test_readiness_records_ci_states(self, state):
        head, run = self.readiness_run()
        self.github.check_state = state
        ready = state == "success"
        run = self.ticks(run, 1 if ready else 3)
        report = self.report(run)
        check = report["current_ci"]
        self.assert_fields(check, operation="ci", head=head, base=run["base_sha"], state=state)
        self.assertEqual((report["ci_checks"], run["stage"], check["readiness_changed"], self.pull()["draft"]),
                         ([check], "ready" if ready else "ci", ready, not ready))
        if ready:
            self.assertFalse(any("CI checks" in item for item in report["limitations"]))
            return
        self.assertIn(CI_FAILED.format("pending"), report["limitations"])
        self.github.check_state = "failure"
        self.assertNotEqual(self.tick_raising(run)["stage"], "ready")
        self.assertTrue(self.pull()["draft"])
        report = self.report(run)
        self.assertEqual(([c["state"] for c in report["ci_checks"]], report["current_ci"]["head"]),
                         (["pending", "failure"], head))
        self.assertIn(CI_FAILED.format("failure"), report["limitations"])

    @scenarios("base", "configuration", "pins")
    def test_ci_observations_become_historical(self, change):
        if change == "base":
            head, run = self.readiness_run()
            self.github.check_state = "pending"
            self.assertEqual(self.ticks(run)["stage"], "ci")
            self.advance_base()
            self.assertEqual(self.tick_raising(run)["stage"], "stale")
        else:
            head, run = self.stopped_review(grants=["github"])
            run = self.ticks(self.select(run, ["checks"]))
            self.assert_fields(self.report(run)["current_ci"], operation="checks", state="success", head=head)
            if change == "pins":
                self.team.invalidate_pins(self.store.project("demo"), run)
            else:
                self.change(change)
            run = self.revalidate(run)
            self.assert_reviewed(run, head)
            self.assertTrue(self.report(run)["current_evidence"])
        report = self.report(run)
        self.assertIsNone(report["current_ci"])
        self.assert_fields(report["ci_checks"][0], head=head, current=False)
        self.assertIn(HISTORICAL_CI, report["limitations"])

    @scenarios(("comment", "-review-", "head"), ("status", "success", "base"), ("comment", "-ready", "head"),
               ("ci", "", "local"), ("ci", "", "dirty"),
               *(("run", "status", kind) for kind in ("head", "base", "configuration", "local", "dirty")))
    def test_movement_around_readiness_never_ready(self, method, trigger, kind):
        head, run = self.readiness_run()
        if method == "run":
            self.store.save_writing({"status": {"instructions": "Write in Spanish."}})
        key = 1 if method in {"ci", "run"} else 2
        with self.moving(self.agents if method == "run" else self.github, method, lambda: self.change(kind, run),
                         lambda *args: trigger in args[key], after=method in {"comment", "status"}), \
                patch.object(self.github, "comment", wraps=self.github.comment) as comment:
            run = self.ticks(run)
        ready = trigger == "-ready"
        self.assertEqual(self.roles(), ["review"] + ["status"] * (method == "run"))
        self.assert_fields(run, stage="stale" if kind in {"head", "base"} else "stopped", reviewed_sha=None,
                           validated_sha=None)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"]), (not ready, ready))
        if ready:
            limitations = self.report(run)["limitations"]
            self.assertIn(STALE_READY.format(head), limitations)
            self.assertFalse(any("did not change draft" in item for item in limitations))
            return
        self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
        self.assertEqual(self.invalidation(run)["head"], head)
        if method != "status":
            self.assert_no_success()
        self.assertNotEqual([s for c, s in self.github.statuses if c == head][-1:], ["success"])
        if method in {"ci", "run"}:
            self.assertFalse([c for c in comment.call_args_list if c.args[2].endswith(("-ready", head))])
        if kind in {"local", "dirty"}:
            self.assert_local_pending(run)

    @scenarios(*zip(INTERRUPTIONS, MOVES), *((None, kind) for kind in MOVES))
    def test_moved_inputs_withhold_repair_adoption_writes(self, point, kind):
        run = self.repair_handoff(grants=["github"])
        head, repair = run["sha"], self.commit("feature", "feature", "repair.txt", "repair\n", "Repair")
        if point:
            self.interrupt(point, lambda: self.adopt_repair(run))
            self.change(kind)
            self.adopt_repair(run)
        else:
            with self.writes("status", lambda state: state == "pending", lambda: self.change(kind)):
                self.adopt_repair(run)
        run = self.reload(run)
        self.assertEqual([(i["type"], i["evidence"]) for i in run["unpublished_evidence"]],
                         [("status", repair)] * bool(point) + [("comment", repair)])
        self.assertEqual(([k for _, k in self.github.comments if "-adopt-" in k],
                          (repair, "pending") in self.github.statuses), ([], not point))
        self.assertEqual((run["outbox"], run["adoptions"][-1]["head"], self.workspace_head(run), self.preserved(run)),
                         ([], repair, repair, [head]))
        self.assert_fields(run, stage="stopped" if kind == "configuration" else "stale", validated_sha=None)

    @scenarios(*MOVES, "pins", "local", "worker", "ready")
    def test_pr_show_verifies_binding(self, change):
        if change == "ready":
            head, run = self.readiness_run()
            run = self.ticks(run)
        else:
            head, run = self.stopped_review()
        report = self.report(run)
        self.assertTrue(report["current_evidence"] and report["independent_review_success"])
        moved = self.change("head" if change == "ready" else change, run)
        busy = self.store.repository_lock("demo") if change == "worker" else nullcontext()
        with self.pinned(change), busy:
            report = self.report(run)
        self.assert_historical(report)
        self.assertIsNone(report["current_ci"])
        self.assert_review(report["review"], head, False)
        self.assertIs(report["currency"]["verified"], None if change == "worker" else False)
        self.assert_limited(report, "not verified" if change == "worker" else "historical")
        self.assertEqual(self.reload(run), run)
        if change == "configuration":
            self.refuses("evidence invalidated", self.select, run, ["review"])
            self.assert_fields(self.reload(run), validated_sha=None, reviewed_sha=None)
        elif change == "head":
            run = self.update(run)
            self.assert_fields(run, sha=moved, review_record=None)
            report = self.report(run)
            historical = self.assert_retired(report, head, run["base_sha"], DELIBERATE, reviewed=head)
            self.assert_review(historical["review"], head, False, agent=run["reviewer"])
            self.assert_review(report["review"], head, False)
        elif change == "base":
            old = run["base_sha"]
            self.assertEqual(self.github.pr(None, 7)["base"]["snapshot_sha"], old)
            run = self.ticks(run)
            self.assert_error(run, "stale", "never merged implicitly")
            self.assert_fields(run, reviewed_sha=None, review_record=None)
            self.assert_retired(self.report(run), head, old, "PR base changed")
            self.assertEqual(len(self.agents.calls), 1)
            run = self.update(run)
            self.assert_fields(run, sha=head, base_sha=moved, reviewed_sha=None)
            self.assertEqual((self.remote_head(), run["adopted_pr"]["base_contained"]), (head, False))
            self.assert_limited(self.report(run), "base was not merged")
            run = self.revalidate(run)
            self.assert_reviewed(run, head, base_sha=moved)
            self.assertTrue(self.report(run)["current_evidence"])

    def test_concurrent_head_change_needs_update(self):
        _, run = self.under_review()
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run)
        self.assert_error(run, "stale", "pr update")
        self.assertEqual(self.agents.calls, [])
        self.refuses("pr update", self.team.refresh, run["id"])
        run = self.update(run)
        self.assert_awaiting(run, sha=external, validated_sha=None)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir() and run["evidence_invalidations"])
        self.assert_fields(self.report(run)["authorship"]["subsequent"][-1], kind="external_update",
                           commit=external, declared=["human"], trailer_families=[])
        self.refuses("unchanged", self.update, run)
        self.assert_reviewed(self.revalidate(run), external)
        self.assertEqual(self.remote_head(), external)

    def test_moved_head_never_overwritten(self):
        run = self.at_publish()
        local = run["sha"]
        external = self.push_external()
        run = self.ticks(run)
        self.assertEqual((run["stage"], self.remote_head(), len(self.agents.calls)), ("stale", external, 2))
        run = self.update(run)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual((update["unpushed_local_commit"], run["sha"], self.head_of(Path(update["preserved"]))),
                         (local, external, local))

    def test_interrupted_push_reconciles_without_force(self):
        run = self.at_publish()
        with patch("agent_team.coordinator.git", side_effect=self.lost_push):
            run = self.ticks(run)
        self.assert_fields(run, stage="blocked", pending_push_sha=run["sha"])
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_reviewed(run, run["sha"], pending_push_sha=None, outbox=[])
        self.assertEqual((self.remote_head(), run["adopted_pr"]["pushed"]), (run["sha"], [run["sha"]]))
        self.assert_status(run["sha"], "pending", PUSHED)

    def lost_push(self, cwd, *args):
        result = self.local_git(cwd, *args)
        if "push" in args:
            self.assertFalse(any(str(a).startswith("+") or a == "--force" for a in args))
            self.assertIn(f"{self.workspace_head(self.store.runs()[0])}:refs/heads/feature", args)
            raise TeamError("Simulated lost push response")
        return result

    @scenarios(None, "comment", "push")
    def test_publishing_local_review(self, crash):
        head = self.open_pr(reject=1)
        run = self.ticks(self.writable(grants=["edit", "github"]), 6)
        sha, key = run["sha"], self.marker(run, run["sha"], 1)
        self.assert_reviewed(run, sha)
        run = self.select(run, ["publish"], ["push", "github"])
        with self.writes("comment", lambda k: crash == "comment" and "-review-" in k, self.crash), \
                patch("agent_team.coordinator.git", side_effect=self.lost_push) if crash == "push" else nullcontext(), \
                self.assertRaises(Interrupted) if crash == "comment" else nullcontext():
            self.ticks(run)
        if crash == "push":
            self.assert_fields(self.reload(run), stage="blocked", pending_push_sha=sha)
            self.assertNotIn(key, self.github.comments)
        if crash and self.ticks(run)["stage"] == "blocked":
            self.team.resume(run["id"])
        run = self.ticks(run)
        self.assert_reviewed(run, sha, published_sha=sha, outbox=[])
        self.assertEqual(self.remote_head(), sha)
        self.assert_status(sha, "pending", "Review reported; readiness not checked")
        self.assert_contains(self.github.comments[key], f"initial PR `{head}`", f"coordinator commit `{sha}`")
        self.assertEqual(self.report(run)["authorship"]["subsequent"][0]["trailer_families"], [FAMILIES[run["author"]]])

    def test_repair_provenance_per_commit(self):
        run = self.repair_handoff(grants=["github"])
        head, family = run["sha"], FAMILIES[run["author"]]
        external = self.push_external(message=trailed("Repair", family))
        self.agents.reject = False
        run = self.revalidate(self.adopt_repair(run))
        self.assert_reviewed(run, external)
        self.assertIn(f"external repair `{external}`: declared human; Agent-Family trailers {family}",
                      self.github.comments[self.marker(run, external, 1)])
        local = self.local_commit(run)
        self.assert_withheld(self.revalidate(run, contributors=["unknown"]), local)
        authorship = self.report(run)["authorship"]
        self.assert_fields(authorship["initial"], commit=head, declared=["human"], trailer_families=[])
        self.assertEqual([(e["kind"], e["commit"], e["declared"], e["trailer_families"], bool(e["commit_authors"]))
                          for e in authorship["subsequent"]], [("external_repair", external, ["human"], [family], True),
                                                               ("local_commit", local, ["unknown"], [], True)])

    @scenarios(True, False)
    def test_permission_lookup_movement_not_pushed(self, moved):
        run = self.at_publish()
        head = self.remote_head()
        with self.moving(self.github, "push_access", lambda: self.local_commit(run)) if moved else \
                patch.object(self.github, "repo", side_effect=unavailable):
            run = self.ticks(run, 2 - moved)
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (head, []))
        if moved:
            self.assert_awaiting(run, validated_sha=None, pending_push_sha=None)
            return self.assert_local_pending(run)
        self.assert_reviewed(run, run["sha"], published_sha=head)
        self.assertIn("could not be verified", self.report(run)["local_handoff"]["reason"])

    @scenarios("failed", "intent", "lost")
    def test_readiness_change_needs_confirmation(self, case):
        head, run = self.readiness_run()
        with self.moving(self.github, "mark_ready", unavailable if case == "failed" else self.crash,
                         after=case == "lost"), suppress(Interrupted):
            self.ticks(run)
        run = self.reload(run)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"], run["readiness_intent"]),
                         (case != "lost", False, head))
        self.assert_limited(self.report(run), "ready was not confirmed")
        run = self.ticks(self.team.resume(self.ticks(run)["id"]))
        self.assert_fields(run, stage="ready", readiness_intent=None)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"]), (False, True))

    def test_stale_ready_intent(self):
        run = self.readiness_run()[1]
        self.pull()["draft"] = False
        self.store.save(run, readiness_intent="0" * 40)
        with patch.object(self.github, "mark_ready") as mark:
            run = self.ticks(run)
        self.assert_fields(run, stage="ready", readiness_intent=None)
        self.assertEqual((mark.call_count, run["ci_checks"][-1]["readiness_changed"],
                          self.report(run)["ci_checks"][-1]["readiness_changed"]), (0, False, False))

    @scenarios(*(("review", kind, grants) for kind in ("local", "dirty") for grants in ([], ["github"])),
               ("ready", "local", None), ("ready", "dirty", None))
    def test_local_change_retires_until_declared(self, when, kind, grants):
        if when == "ready":
            head, run = self.readiness_run()
            run = self.ticks(run)
            self.assertEqual(run["stage"], "ready")
            baseline = run["evidence_context"]
            self.change(kind, run)
            run = self.ticks(run, 3)
            self.assertEqual([e["reason"] for e in run["evidence_invalidations"]].count(LOCAL_CHANGE), 1)
            self.assert_status(head, "pending", "Evidence inputs changed; readiness invalidated")
            self.assertIn(STALE_READY.format(head), self.report(run)["limitations"])
        else:
            head, run = self.under_review(grants=grants)
            baseline = run["evidence_context"]
            run = self.tick_moving_during_review(run, lambda: self.change(kind, run))
            self.assertEqual(run["outbox"], [])
            self.assert_quiet()
        self.assert_awaiting(run, reviewed_sha=None, validated_sha=None, evidence_context=baseline)
        self.assertTrue(run["pending_contribution"])
        self.assert_retired(self.report(run), head, run["base_sha"], LOCAL_CHANGE, reviewed=head)
        self.refuses("contributor declarations", self.select, run, RECHECK)
        if kind == "local":
            run = self.revalidate(run, contributors=["human"])
            self.assert_reviewed(run, self.workspace_head(run))
            self.assertNotEqual(run["reviewed_sha"], head)

    @scenarios(("openai", "codex", "claude"), ("anthropic", "claude", "codex"))
    def test_interrupted_adoption_keeps_explicit_roles(self, trailer, author, reviewer):
        if trailer == "openai":
            self.store.save(self.store.create(self.project, self.github.items[0]), stage="closed")
        self.open_pr(families=[trailer])
        real = self.store.save

        def crash(record, **changes):
            if "branch" in changes:
                raise TeamError("Interrupted")
            real(record, **changes)

        with patch.object(self.store, "save", side_effect=crash):
            self.refuses("Interrupted", self.adopt_pr)
        run = next(r for r in self.store.runs() if r.get("adopted_pr"))
        self.assert_fields(run, author=author, reviewer=reviewer)
        self.assertTrue(run["independence"]["established"])

    def test_validation_reports_omitted_commands(self):
        self.configure(tests=["false", "true", "echo skipped"])
        head = self.open_pr()
        run = self.reviewed(grants=["github"])
        self.assert_awaiting(run, "implement")
        report = self.report(run)
        checks = report["validation"]["checks"]
        self.assertEqual([(c["command"], c["performed"], c.get("exit_code")) for c in checks],
                         [("false", True, 1), ("true", False, None), ("echo skipped", False, None)])
        self.assertEqual(report["review"]["validation_checks"], checks)
        self.assert_limited(report, "omitted after an earlier failure: true, echo skipped")
        body = self.github.comments[self.marker(run, head)]
        self.assertEqual(body.count("omitted after an earlier failure"), 2)
        self.assertIn("exit 1", body)

    @scenarios(None, "base", "configuration", "pins")
    def test_queued_review_comment_after_changes(self, change):
        head, run = self.under_review(grants=["github"])
        with patch.object(self.github, "comment", side_effect=unavailable), self.assertRaises(TeamError):
            self.ticks(run)
        run = self.reload(run)
        self.assertEqual((run["stage"], len(run["outbox"])), ("stopped", 1))
        self.store.save(run, readiness_intent=head)
        self.change(change)
        with self.pinned(change):
            run = self.ticks(run)
        self.assertEqual((run["outbox"], len(self.agents.calls)), ([], 1))
        if change is None:
            self.assertIn(self.marker(run, head), self.github.comments)
            return
        self.assertNotIn(self.marker(run, head), self.github.comments)
        self.assertEqual(run["unpublished_evidence"][0]["evidence"], head)
        if change == "base":
            self.assert_error(run, "stale", "PR base changed")
            return
        reason = REASONS[change]
        self.assertEqual((run["stage"], run["next_stage"], self.github.comments, run.get("readiness_intent"),
                          run["unpublished_evidence"][0]["withheld_reason"]), ("stopped", "validate", {}, None, reason))
        report = self.report(run)
        self.assert_review(report["review"], head, False)
        self.assertEqual(self.assert_retired(report, head, run["base_sha"], reason)["reason"], reason)
        self.refuses("validat", self.select, run, ["review"])
        self.assertEqual(len(self.agents.calls), 1)

    @scenarios("head", "local", "handoff", "closed", "merged")
    def test_movement_during_review_retires_evidence(self, case):
        if case == "handoff":
            self.configure(max_revisions=0)
        base, rejected = self.remote_head("main"), case in {"head", "handoff"}
        head, run = self.under_review(rejected, grants=[] if case == "local" else ["github"])
        close = lambda: self.pull().update(state="closed", merged=case == "merged")
        run = self.tick_moving_during_review(run, close if case in {"closed", "merged"} else None)
        report = self.report(run)
        self.assertFalse(report["current_evidence"])
        self.assert_review(report["review"], head, False, "changes_requested" if rejected else "pass")
        self.assertEqual(self.github.comments, {})
        if case == "head":
            self.assert_error(run, "stale", "PR head moved", "before review evidence was published")
            self.assertNotIn((head, "failure"), self.github.statuses)
            self.assertEqual((run["outbox"], {i["evidence"] for i in run["unpublished_evidence"]},
                              len(report["unpublished_evidence"])), ([], {head}, 2))
            self.assert_limited(report, "was not published")
            run = self.ticks(run, 2)
            self.assertEqual((run["stage"], self.github.comments, len(self.agents.calls)), ("stale", {}, 1))
            self.assert_awaiting(self.update(run))
        elif case == "local":
            self.assert_error(run, "stale", "PR head moved")
            self.assert_fields(run, validated_sha=None, reviewed_sha=None)
            self.assertEqual((self.github.statuses, report["validation"]["results"]), ([], []))
            self.assert_limited(report, "historical")
            historical = self.assert_retired(report, head, base, "PR head moved", validated=head,
                                             reviewed=head, independent_review_success=True)
            for review in (historical["review"], report["review"]):
                self.assert_review(review, head, False, base=base, agent=run["reviewer"])
                self.assertTrue(review["summary"])
                self.assertIn("findings", review)
        elif case == "handoff":
            self.assert_fields(run, stage="stale", handoffs=None, round=0, outbox=[])
            self.assertEqual((run.get("rejected_shas", []), self.github.statuses), ([], []))
            self.assertIn(self.marker(run, head)[1], [i.get("marker") for i in run["unpublished_evidence"]])
            self.assertIn("PR head moved", run["evidence_retired"]["reason"])
            self.refuses("handoff", self.team.decide, run["id"], "repair")
        else:
            self.assert_fields(run, stage=case, reviewed_sha=None, outbox=[])
            self.assert_retired(report, head, run["base_sha"], "")
            self.assertEqual(self.pull()["state"], "closed")

    def test_outbox_movement_withholds_later_evidence(self):
        head, run = self.under_review(True, grants=["github"])
        with self.writes("comment", lambda key: "-review-" in key, self.push_external):
            run = self.ticks(run)
        self.assertIn(self.marker(run, head), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual((run["stage"], run["outbox"], len(self.agents.calls), self.invalidation(run)["head"],
                          [i["type"] for i in run["unpublished_evidence"]]), ("stale", [], 1, head, ["status"]))
        for reason in (run["error"], self.invalidation(run)["reason"]):
            self.assertIn("PR head moved", reason)

    @scenarios("configuration", "local", "dirty", "head", "base")
    def test_moved_rejection_is_history_only(self, kind):
        self.configure(max_revisions=0)
        base = self.remote_head("main")
        head, run = self.under_review(True, grants=["github"])
        run = self.tick_moving_during_review(run, lambda: self.change(kind, run))
        self.assert_fields(run, round=0, handoffs=None, revision_history=None, review_record=None, outbox=[],
                           stage="stale" if kind in {"head", "base"} else "stopped")
        self.assertEqual((run.get("rejected_shas", []), bool(run.get("needs_revision"))), ([], False))
        self.assert_quiet()
        self.assertEqual({i["type"] for i in run["unpublished_evidence"]}, {"comment", "status"})
        self.assert_retired(self.report(run), head, base, REASONS[kind], verdict="changes_requested")
        if kind == "configuration":
            self.agents.reject = False
            self.assert_reviewed(self.revalidate(run), head)

    @scenarios("failed", "crash", "moved")
    def test_post_push_status_survives(self, case):
        run = self.at_publish()
        real_save = self.store.save

        def save(record, **changes):
            real_save(record, **changes)
            if "published_sha" in changes and changes.get("outbox"):
                raise Interrupted()

        with {"failed": self.moving(self.github, "status", unavailable,
                                    lambda *args: args[-1].startswith("Revision pushed")),
              "crash": patch.object(self.store, "save", side_effect=save),
              "moved": self.moving(coordinator, "git", self.push_external, lambda _, *args: "push" in args,
                                   after=True)}[case]:
            try:
                self.ticks(run)
            except (Interrupted, TeamError):
                pass
        run = self.reload(run)
        pushed = run["published_sha"]
        self.assertEqual((self.remote_head() == pushed, run["adopted_pr"]["pushed"]), (case != "moved", [pushed]))
        if case == "moved":
            descriptions = [(s, d) for s, _, d in self.github.status_descriptions if s == pushed]
            self.assertEqual((run["stage"], run["outbox"], descriptions,
                              [i["description"] for i in run["unpublished_evidence"]]), ("stale", [], [], [PUSHED]))
            return
        self.ticks(self.team.resume(run["id"]) if case == "failed" else run)
        if case == "crash":
            self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_reviewed(run, pushed, outbox=[])
        descriptions = [d for s, _, d in self.github.status_descriptions if s == pushed]
        self.assertIn(PUSHED, descriptions)
        self.assertNotEqual(descriptions[-1], "Coordinator blocked; maintainer action needed")

    @scenarios(*((change, "update") for change in (*MOVES, "local")), ("local", "resume"))
    def test_stale_persisted_rejection_retired(self, change, entry):
        head = self.open_pr(reject=True)
        run = self.ticks(self.editing(), 2)
        with patch.object(self.team, "record_review", side_effect=TeamError("Interrupted")):
            run = self.ticks(run)
        self.assert_fields(run, stage="blocked", review_sha=head)
        self.change(change, run)
        try:
            self.update(run) if entry == "update" else self.ticks(self.team.resume(run["id"]))
        except TeamError:
            pass
        run = self.reload(run)
        self.assert_fields(run, round=0, review_record=None, needs_revision=False, revision_history=None)
        self.assertEqual((run.get("rejected_shas", []), self.roles(),
                          [(h["head"], h["verdict"]) for h in self.report(run)["historical_evidence"] if h["review"]]),
                         ([], ["review"], [(head, "changes_requested")]))
        if change in {"head", "base"}:
            self.assert_awaiting(run)

    def test_review_history_keeps_both_rejections(self):
        self.configure(max_revisions=1, tests=["! grep -q fixed feature.txt"])
        head = self.open_pr(reject=1)
        run = self.until_handoff(self.editing())
        self.assertEqual([e["kind"] for e in run["revision_history"]], ["review", "validation"])
        report = self.report(run)
        for review in (report["review"], *report["review_history"]):
            self.assert_review(review, head, False, "changes_requested", run["base_sha"], run["reviewer"])
            self.assert_fields(review, candidate_verdict="changes_requested", validation_failed=False)

    def test_continuation_and_drift_keep_evidence(self):
        head, run = self.stopped_review()
        local = self.local_commit(run)
        run = self.select(run, RECHECK, contributors=["human"])
        self.assert_review(self.report(run)["review"], head, False, agent=run["reviewer"])
        run = self.ticks(run, 2)
        report = self.report(run)
        historical = report["historical_evidence"][-1]
        self.assert_fields(historical, reason=DRIFT, validated=head, reviewed=head)
        self.assert_review(historical["review"], head, False, base=run["base_sha"], agent=run["reviewer"])
        self.assertTrue(historical["review"]["summary"] and report["independent_review_success"])
        self.assert_review(report["review"], local, True)
        self.store.save(run, stage="closed")
        head = self.open_pr(8, "drift")
        run = self.reviewed("8", count=2)
        (self.store.workspace(run) / "stray.txt").write_text("operator edit\n")
        run = self.ticks(run)
        self.assert_awaiting(run, validated_sha=None)
        self.assert_retired(self.report(run), head, run["base_sha"], DRIFT, verdict=None, validated=head)
        self.assertEqual(self.roles(), ["review", "review"])

    @scenarios("identity", "repository")
    def test_head_identity_change_retires_evidence(self, change):
        head, run = self.stopped_review(grants=["github"])
        self.change(change)
        run = self.ticks(run)
        self.assert_error(run, "stale", "head repository or branch changed")
        self.assert_fields(run, validated_sha=None, reviewed_sha=None)
        report = self.report(run)
        self.assertFalse(report["independent_review_success"])
        self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
        self.assert_status(head, "pending", "PR head changed; review invalidated")
        self.refuses("adopt the PR again", self.update, run)

    @scenarios("repository", "identity")
    def test_repair_refuses_changed_head_identity(self, change):
        run = self.repair_handoff()
        before = self.workspace_head(run)
        self.change(change)
        self.refuses("head repository or branch changed", self.adopt_repair, run)
        after = self.reload(run)
        self.assert_fields(after, stage="repair", adoptions=None, adopted_pr=run["adopted_pr"])
        self.assertEqual((self.workspace_head(after), list(self.store.run_root(after).glob("refresh-*"))), (before, []))

    def test_update_continuation_stops_on_rejection(self):
        head, run = self.stopped_review(self.writable)
        external = self.push_external()
        self.assertEqual(self.ticks(run)["stage"], "stale")
        self.update(run)
        run = self.select(run, RECHECK)
        self.assert_fields(run, pr_followup=None, released_pr_followup=FOLLOWUP)
        self.agents.reject = True
        run = self.ticks(run, 4)
        self.assert_awaiting(run, "implement", sha=external)
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual((self.roles(), self.remote_head(), run["adopted_pr"].get("pushed", [])),
                         (["review", "review"], external, []))

    @scenarios(("revise", ALL), ("review", ["github"]))
    def test_failing_head_is_reviewed_before_any_edit(self, mode, grants):
        head = self.open_pr()
        self.configure(tests=["grep -q fixed feature.txt"])
        run = self.ticks(self.writable(mode, grants), 2)
        self.assert_fields(run, stage="review", validated_sha=None)
        self.assertEqual((run["tests"][0]["exit_code"], run.get("rejected_shas", []), self.agents.calls), (1, [], []))
        run = self.ticks(run)
        entry = run["revision_history"][0]
        self.assert_fields(entry, sha=head, kind="review", validation_failed=True)
        self.assertEqual((self.agents.calls, entry["findings"][0]["severity"], self.remote_head()),
                         ([(run["reviewer"], "review")], "validation", head))
        self.assert_status(head, "failure", "Configured validation failed")
        self.assertIn("exit 1", self.github.comments[self.marker(run, head)])
        if mode == "revise":
            self.assertEqual(run["stage"], "revision")
            run = self.ticks(run, 4)
            self.assert_revised(run, head, validated_sha=run["sha"])
            return
        self.assert_awaiting(run, "implement")
        report = self.report(self.assert_stop_boundary(run, head))
        review = report["review"]
        self.assert_review(review, head, True)
        self.assert_fields(review, summary=self.agents.summary, findings=[], validation_failed=True,
                           candidate_verdict="changes_requested")
        self.assertEqual(review["candidate_findings"][0]["severity"], "validation")
        self.assertFalse(report["validation"]["passed_for_candidate"] or report["independent_review_success"])

    @scenarios(("external_repair", "unknown", ["human"]), ("external_repair", "gemini", ["human"]),
               ("external_repair", None, ["unknown"]), ("local_commit", "gemini", ["human"]))
    def test_unresolved_trailers_withhold_independence(self, kind, value, declared):
        unresolved = [value] if value else []
        if kind == "local_commit":
            head, run = self.stopped_review(self.editing)
            commit = self.local_commit(run, trailed("Local change", value))
            run = self.select(run, RECHECK, contributors=declared)
            self.assert_not_independent(run)
            self.assert_fields(run, unresolved_trailers=unresolved, reviewed_sha=None)
            self.assertEqual(run["adopted_pr"]["unresolved_trailers"], unresolved)
            self.assert_withheld(self.ticks(run, 2), commit)
            self.change("dirty", run)
            self.refuses("Independent review cannot", self.select, run, RECHECK, ALL, contributors=declared)
            git(self.store.workspace(run), "checkout", ".")
            self.assert_error(self.ticks(self.select(run, ["publish"], ["push", "github"])), "blocked", "nothing was pushed")
            self.assertEqual(self.remote_head(), head)
        else:
            run = self.repair_handoff()
            self.assertTrue(run["independence"]["established"])
            commit = self.push_external(message=trailed("Repair", value))
            run = self.adopt_repair(run, declared)
            self.assertEqual(run["sha"], commit)
            self.assert_not_independent(run, value or "unknown contributors")
            self.assertEqual((run["adopted_pr"]["unresolved_trailers"], run["adoptions"][-1]["unresolved_trailers"]),
                             (unresolved, unresolved))
            self.assertEqual(run["contributors"], sorted({"human", *declared}))
            self.refuses_revision("Independent review cannot be established", run)
            self.agents.reject = False
            self.assert_withheld(self.revalidate(run, contributors=declared), commit, unresolved)
            self.assertNotIn("implement", self.roles())
            self.assert_fields(self.report(run)["authorship"]["initial"], declared=["human"], unresolved_trailers=[])
        self.assert_fields(self.report(run)["authorship"]["subsequent"][-1], kind=kind, commit=commit,
                           declared=declared, unresolved_trailers=unresolved)

    @scenarios(*((command, point, closing) for command in ("pr update RUN_ID", "adopt RUN_ID")
                 for point in INTERRUPTIONS for closing in (False, True)))
    def test_interrupted_swap_recovers_inputs(self, command, point, closing):
        updating = command == "pr update RUN_ID"
        run, head, external, recover = self.interrupted_swap(updating, point)
        stored = self.reload(run)
        if closing:
            self.refuses("interrupted", self.command, "close", run["id"])
            self.assertEqual(self.reload(run)["stage"], stored["stage"])
            if point == "rename-2":
                self.store.save(self.reload(run), stage="closed")
            run = recover()
            self.assert_fields(run, pending_swap=None, sha=external, stage="closed" if point == "rename-2" else "stopped")
            self.command("close", run["id"])
            return self.assert_fields(self.reload(run), stage="closed", pending_swap=None)
        self.assert_fields(stored, stage="stale" if updating else "repair", sha=head)
        self.assertEqual(stored["pending_swap"]["command"], command)
        if updating:
            self.refuses("interrupted", self.select, run, ["validate"])
            self.refuses("interrupted", self.team.resume, run["id"])
        else:
            self.refuses("interrupted", self.team.decide, run["id"], "stop")
            later = self.commit("feature", "feature", "later.txt", "later\n", "Later change")
        run = recover()
        self.assert_awaiting(run, pending_swap=None, sha=external, validated_sha=None, reviewed_sha=None)
        self.assertEqual((self.workspace_head(run), run["adopted_pr"]["head_sha"], self.preserved(run), self.remote_head()),
                         (external, external, [head], external if updating else later))
        if updating:
            self.assertEqual((run["evidence_context"]["head"], self.invalidation(run)["reason"]),
                             (external, DELIBERATE))
            self.assert_reviewed(self.revalidate(run), external)
            return
        self.assertEqual(([(a["head"], a["declared"]) for a in run["adoptions"]], run["round"]),
                         ([(external, ["human"])], 1))
        self.assert_error(self.ticks(run), "stale", "agent-team adopt")

    def test_supplied_findings_get_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=FIX)
        self.assertEqual(run["operations"], FOLLOWUP)
        run = self.ticks(run, 5)
        self.assertEqual((run["stage"], run.get("rejected_shas", [])), ("stopped", []))
        self.assert_contains(self.agents.prompts["implement"], FIX[0], "existing PR #7")
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"], self.remote_head()), (run["sha"],) * 3)
        self.assertTrue(self.pull()["draft"])

    def test_findings_first_edit_uses_budget(self):
        self.configure(max_revisions=0)
        self.open_pr()
        self.refuses(NO_BUDGET, self.editing_findings)
        self.assertEqual((self.store.runs(), self.agents.calls), ([], []))
        self.configure(max_revisions=1)
        self.agents.reject = True
        run = self.editing_findings()
        self.assert_fields(run, round=1, revision_limit=1)
        run = self.until_handoff(run)
        self.assertEqual((run["stage"], run["round"], self.roles().count("implement")), ("handoff", 1, 1))
        self.store.save(run, stage="stopped", next_stage="revision", round=2, needs_revision=True)
        self.refuses_revision(NO_BUDGET, run)
        self.assertEqual(self.roles().count("implement"), 1)

    def test_exhausted_repair_adopted_without_merge(self):
        run = self.repair_handoff()
        external = self.push_external()
        self.advance_base()
        run = self.adopt_repair(run)
        self.assert_awaiting(run, sha=external)
        self.assert_fields(run["adopted_pr"], head_sha=external, base_contained=False)
        self.assertEqual(self.remote_head(), external)

    @scenarios(0, 2)
    def test_author_entry_needs_reserved_round(self, limit):
        self.configure(max_revisions=limit)
        head, run = self.stopped_review(self.writable)
        self.assertEqual(run["round"], 0)
        for operations in (["implement", *RECHECK], REVISION):
            self.refuses("No revision round is reserved", self.select, run, operations, ["edit"])
        self.store.save(run, round=limit + 1, reserved_round=limit + 1, needs_revision=True)
        self.refuses(NO_BUDGET, self.select, run, ["implement", "validate"], ["edit"])
        self.assertEqual((self.roles(), self.remote_head()), (["review"], head))

    def test_findings_stop_on_unrelated_findings(self):
        self.configure(max_revisions=3)
        self.open_pr(reject=1)
        self.agents.findings = [[{"severity": "P2", "location": "other.txt:3", "evidence": "Unrelated",
                                  "request": "Rewrite other.txt"}]]
        run = self.writable("findings", findings=FIX)
        self.assertIsNone(run["pr_followup"])
        run = self.ticks(run, 6)
        self.assert_awaiting(run, "implement", round=2)
        self.assertEqual(self.roles(), ["implement", "review"])
        self.assertIn("supplied findings remain the revision scope", run["partial_result"])
        report = self.report(run)
        self.assertEqual((report["requested_findings"], report["review"]["findings"][0]["location"]),
                         (FIX, "other.txt:3"))
        self.store.save(run, round=1, reserved_round=1)
        self.refuses_revision("each round allows one pass", run)
        self.assertEqual(len(self.agents.calls), 2)

    @scenarios((["openai"], ["human"], "cannot review independently"),
               (["human", "unknown"], ["human"], "unknown contributors"),
               (["openai"], ["anthropic"], "both model families"))
    def test_readoption_carries_provenance(self, first, second, message):
        self.open_pr()
        run = self.reviewed(contributors=first)
        self.store.save(run, stage="closed")
        self.push_external()
        self.refuses(message, self.editing, contributors=second, reviewer="codex")
        again = self.adopt_pr(contributors=second)
        self.assertEqual(again["adopted_pr"]["inherited_provenance"]["runs"], [run["id"]])
        self.assertTrue(set(first) <= set(again["contributors"]))
        if first == ["openai"] and second == ["human"]:
            self.assert_fields(again, author="codex", reviewer="claude")
        else:
            self.assertIn(message, again["independence"]["reason"])

    @scenarios(True, False)
    def test_large_pr_needs_complete_patch(self, compact_fits):
        lines = [f"line {n}" for n in range(300)]
        self.commit("main", "main", "big.txt", "\n".join(lines) + "\n", "Add big file")
        self.open_pr()
        edited = [f"edited {n}" if n % 50 == 0 else line for n, line in enumerate(lines)]
        head = self.commit("feature", "feature", "big.txt", "\n".join(edited) + "\n", "Edit big file")
        span = f"{self.remote_head('main')}...{head}"
        default, compact = (len(git(self.remote, "diff", *options, span)) + 1 for options in ([], ["--unified=0"]))
        with patch("agent_team.patches.REVIEW_BUDGET", compact if compact_fits else compact - 1):
            run = self.tick_raising(self.adopt_pr(), 3)
        if not compact_fits:
            self.assert_error(run, "blocked", "exceeds review budget even without context")
            self.assert_fields(run, reviewed_sha=None, review_record=None)
            self.assertEqual(self.agents.calls, [])
            return
        self.assert_reviewed(run, head)
        self.assert_fields(run["review_record"]["patch"], format="compact", complete=True, files=2,
                           changed_lines=13, characters=compact, default_characters=default, range=span)
        prompt = self.agents.prompts["review"]
        self.assert_contains(prompt, COMPACT_NOTICE, "-line 50\n+edited 50\n")
        self.assertNotIn("\n line 51\n", prompt)
        self.assertEqual(self.report(run)["review"]["patch"], run["review_record"]["patch"])
        self.assertIn("complete context-free patch", review_comment(head, run["review_record"]))

    def test_interrupted_ready_success_revoked(self):
        head, run = self.readiness_run()
        with self.writes("status", lambda state: state == "success", self.crash), self.assertRaises(Interrupted):
            self.ticks(run)
        self.assertEqual(self.github.statuses[-1], (head, "success"))
        self.advance_base()
        self.assert_fields(self.ticks(run), stage="stale", readiness_status=None)
        self.assertEqual(self.github.statuses[-1], (head, "pending"))

    @scenarios("rename-1", "metadata", "tampered")
    def test_interrupted_preparation_recovers(self, point):
        head = self.open_pr()
        run = self.adopt_pr()
        self.interrupt("metadata" if point == "metadata" else "rename-1", lambda: self.ticks(run))
        if point == "tampered":
            self.local_commit(run)
        self.ticks(run)
        self.refuses("Initial preparation was interrupted", self.update, run)
        run = self.ticks(self.team.resume(run["id"]), 3)
        if point == "tampered":
            self.assertIn("differs from the adopted PR head", run["error"])
            return
        self.assert_reviewed(run, head, installing=None)
        self.assertEqual(self.roles(), ["review"])

    def test_exhausted_fork_repaired_locally(self):
        self.configure(max_revisions=1)
        base, head = self.remote_head("main"), self.open_pr(8, "fork-feature", head_repo=FORK, reject=2)
        run = self.until_handoff(self.adopt_pr("revise", "8", grants=ALL))
        rejected = run["sha"]
        self.assertIn("local repair checkout", run["handoffs"][-1]["text"])
        checkout = Path(self.team.decide(run["id"], "repair")["repair_checkout"]["path"])
        self.assertEqual(self.head_of(checkout), rejected)
        (checkout / "repair.txt").write_text("repair\n")
        git(checkout, "add", ".")
        git(checkout, *COMMIT, "commit", "-m", "Repair")
        repaired = self.head_of(checkout)
        run = self.adopt_repair(run)
        self.assert_awaiting(run, sha=repaired, base_sha=base, published_sha=head, pending_swap=None)
        self.assertTrue(run["adoptions"][-1]["local"] and rejected in run["rejected_shas"])
        self.agents.reject = False
        run = self.revalidate(run)
        self.assert_reviewed(run, repaired)
        self.assertEqual(self.report(run)["local_handoff"]["commit"], repaired)
        self.assert_untouched(head, 8, "fork-feature")

    @scenarios(None, "head", "identity", "retarget")
    def test_exhausted_fork_repair_adopts_moved_base(self, movement):
        self.configure(max_revisions=1)
        old, head = self.remote_head("main"), self.open_pr(8, "fork-feature", head_repo=FORK, reject=2)
        run = self.team.decide(self.until_handoff(self.adopt_pr("revise", "8", grants=ALL))["id"], "repair")
        rejected, checkout = run["sha"], Path(run["repair_checkout"]["path"])
        (checkout / "repair.txt").write_text("repair\n")
        git(checkout, "add", ".")
        git(checkout, *COMMIT, "commit", "-m", "Repair")
        repaired = self.head_of(checkout)
        base = self.advance_base()
        self.refuses("pr update RUN_ID", self.adopt_repair, run)
        (checkout / "draft.txt").write_text("unfinished\n")
        if movement == "head":
            self.push_external("fork-feature")
        elif movement == "identity":
            git(self.remote, "branch", "other-branch", "fork-feature")
            self.pull(8)["branch"] = "other-branch"
        elif movement == "retarget":
            git(self.remote, "branch", "release", "main")
            self.pull(8)["base"] = "release"
        before = self.reload(run)
        if movement:
            self.refuses({"head": "PR head moved", "identity": "head repository or branch changed",
                          "retarget": "never retargets"}[movement], self.update, run)
        else:
            self.update(run)
        after = self.reload(run)
        # The repair checkout, its uncommitted work, the candidate, budget and history are untouched.
        self.assertEqual((self.head_of(checkout), (checkout / "draft.txt").read_text(), self.workspace_head(after)),
                         (repaired, "unfinished\n", rejected))
        kept = ("stage", "sha", "published_sha", "pr", "round", "revision_limit", "handoffs", "decisions",
                "rejected_shas", "repair_checkout", "grants", "contributors", "provenance", "independence",
                "revision_history")
        self.assertEqual({k: after.get(k) for k in kept}, {k: before.get(k) for k in kept})
        if movement:
            self.assertEqual(after, before)
            return
        self.assert_fields(after, base_sha=base, validated_sha=None, reviewed_sha=None)
        self.assert_fields(after["adopted_pr"], number=8, head_repo=FORK, head_ref="fork-feature", head_sha=head,
                           base_sha=base, base_contained=False)
        self.assert_fields(after["adopted_pr"]["updates"][-1], merged=False, base_fast_forward=True,
                           repair_head=repaired, before={"head": head, "base": old, "candidate": rejected},
                           after={"head": head, "base": base})
        invalidation = self.invalidation(after)
        self.assertEqual((invalidation["head"], invalidation["base"]), (rejected, old))
        self.assertIn("base during local repair", invalidation["reason"])
        self.refuses("unchanged", self.update, after)
        self.refuses("Commit or remove", self.adopt_repair, after)
        (checkout / "draft.txt").unlink()
        run = self.adopt_repair(after)
        self.assert_awaiting(run, sha=repaired, base_sha=base, published_sha=head, validated_sha=None,
                             reviewed_sha=None)
        self.assertEqual((run["round"], run["adoptions"][-1]["base_sha"]), (before["round"] + 1, base))
        self.agents.reject = False
        run = self.revalidate(run)
        self.assert_reviewed(run, repaired, base_sha=base)
        # Review received the exact adopted base, while the candidate still does not contain it.
        self.assertIn(f"Base {base}; candidate {repaired}.", self.agents.prompts["review"])
        workspace = self.store.workspace(run)
        self.assertEqual((self.workspace_head(run), git(workspace, "rev-parse", "refs/agent-team/base")),
                         (repaired, base))
        self.assertNotEqual(git(workspace, "merge-base", base, repaired), base)
        report = self.report(run)
        self.assert_limited(report, "base was not merged")
        self.assertEqual(report["local_handoff"]["commit"], repaired)
        self.assert_untouched(head, 8, "fork-feature")

    def test_failed_reconciliation_notification_keeps_ci(self):
        head, run = self.readiness_run()
        self.assertEqual(self.ticks(run)["stage"], "ready")
        self.github.check_state = "failure"
        with self.moving(self.github, "status", unavailable,
                         lambda *args: args[-1] == "CI changed; waiting for checks"):
            run = self.tick_raising(run)
        # The failure was recorded before the notification failed, so it is reported as current.
        self.assert_fields(run, stage="ci")
        self.assertEqual(run["outbox"][0]["description"], "CI changed; waiting for checks")
        report = self.report(run)
        self.assert_fields(report["current_ci"], operation="reconcile", state="failure", head=head,
                           base=run["base_sha"], generation=run.get("evidence_generation", 0))
        self.assertEqual([(c["operation"], c["state"], c["readiness_changed"]) for c in report["ci_checks"]],
                         [("ci", "success", True), ("reconcile", "failure", False)])
        self.assert_contains(report["limitations"], CI_FAILED.format("failure"), STALE_READY.format(head))
        # A later success keeps the transient failure as history.
        self.github.check_state = "success"
        run = self.ticks(run)
        self.assert_fields(run, stage="ready", outbox=[])
        self.assert_status(head, "pending", "CI changed; waiting for checks")
        self.assertEqual(self.github.statuses[-1], (head, "success"))
        report = self.report(run)
        self.assertEqual([(c["operation"], c["state"]) for c in report["ci_checks"]],
                         [("ci", "success"), ("reconcile", "failure"), ("ci", "success")])
        self.assert_fields(report["current_ci"], operation="ci", state="success", head=head)
        self.assertFalse(any("CI checks" in item for item in report["limitations"]))

    def test_cli_parses_existing_pr_operations(self):
        parse = parser().parse_args
        args = parse(["pr", "review", "demo", "7", "--contributor", "unknown"])
        self.assertEqual((args.pr_command, args.pull, args.contributor), ("review", "7", ["unknown"]))
        args = parse(["pr", "findings", "demo", "https://github.com/example/demo/pull/7",
                      "--contributor", "human", "--grant", "edit", "--finding", "Fix the parser"])
        self.assertEqual((args.finding, parse(["pr", "update", "RUN", "--contributor", "human"]).run_id),
                         (["Fix the parser"], "RUN"))
        with self.assertRaises(SystemExit):
            parse(["pr", "review", "demo", "7", "--contributor", "human", "--grant", "readiness"])
        self.assertEqual(parse(["adopt", "RUN", "--contributor", "unknown"]).contributor, ["unknown"])
        self.refuses("Unknown contributor", self.team.select, "demo", ["validate"], [], task="Validate", ref="main",
                     contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
