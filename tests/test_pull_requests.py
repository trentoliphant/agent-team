"""Adoption of existing pull requests. Local Git fixtures and fake providers only; no model or GitHub calls."""
from contextlib import nullcontext
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from agent_team.cli import parser
from agent_team.coordinator import Coordinator, pull_number
from agent_team.github import GitHub
from agent_team.process import git, TeamError

# A module import keeps discovery from running WorkflowTests here again.
from tests import test_coordinator
from tests.test_coordinator import FakeGitHub

COMMIT = ["-c", "user.name=Human", "-c", "user.email=human@example.invalid", "-c", "commit.gpgsign=false"]
ALL = ["edit", "push", "github"]
HISTORICAL_CI = "GitHub CI checks were not checked for the current candidate and base; earlier observations are historical."


class PullGitHub(FakeGitHub):
    """Pull requests whose heads are branches in the local bare remote, including simulated forks."""

    def __init__(self, remote):
        super().__init__(remote)
        self.pulls = {}
        self.permissions = {}

    def pr(self, repo, number):
        if number not in self.pulls:
            return super().pr(repo, number)
        p = self.pulls[number]
        return {"number": number, "title": p["title"], "body": p["body"], "state": p["state"],
                "merged": p["merged"], "draft": p["draft"], "user": {"login": p["user"]},
                "html_url": f"https://github.com/example/demo/pull/{number}",
                "maintainer_can_modify": p["maintainer_can_modify"],
                "head": {"sha": git(self.remote, "rev-parse", f"refs/heads/{p['branch']}"), "ref": p["branch"],
                         "repo": {"full_name": p["head_repo"]}},
                "base": {"ref": p["base"], "sha": git(self.remote, "rev-parse", f"refs/heads/{p['base']}")}}

    def repo(self, name):
        return {"full_name": name, "permissions": {"push": self.permissions.get(name, False)}}

    def push_access(self, project, pr):
        return GitHub.push_access(self, project, pr)

    def mark_ready(self, repo, number):
        if number not in self.pulls:
            return super().mark_ready(repo, number)
        self.pulls[number]["draft"] = False


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        # The workflow fixture, with pull requests and fetches of their refs/pull heads.
        test_coordinator.WorkflowTests.setUp(self)
        self.source = self.root / "source"
        self.github = PullGitHub(self.remote)
        self.team = Coordinator(self.store, self.github, self.agents)
        self.git_patch.stop()
        self.git_patch = patch("agent_team.coordinator.git", side_effect=self.local_git)
        self.git_patch.start()

    def tearDown(self):
        test_coordinator.WorkflowTests.tearDown(self)

    def scenarios(self, *cases):
        """Run each case as a subtest on a fresh fixture, so commits and runs never leak between cases."""
        for case in cases:
            with self.subTest(case=case):
                self.tearDown()
                self.setUp()
                yield case

    def local_git(self, cwd, *args):
        args = list(args)
        if "push" in args:
            args[args.index("push") + 1] = str(self.remote)
        if "fetch" in args:
            args = [str(self.remote) if str(a).startswith("https://github.com/") else a for a in args]
            # GitHub exposes every PR head, including fork heads, as refs/pull/N/head on the base repository.
            args = [f"refs/heads/{self.github.pulls[int(m[1])]['branch']}"
                    if (m := re.fullmatch(r"refs/pull/(\d+)/head", str(a))) else a for a in args]
        return git(cwd, *args)

    def commit(self, branch, start, name, text, message):
        """Push a human commit on top of the remote's `start` branch to `branch`."""
        git(self.source, "fetch", str(self.remote), start)
        git(self.source, "checkout", "-B", branch, "FETCH_HEAD")
        (self.source / name).write_text(text)
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", message)
        git(self.source, "push", str(self.remote), f"HEAD:refs/heads/{branch}")
        return git(self.source, "rev-parse", "HEAD")

    def open_pr(self, number=7, branch="feature", head_repo="example/demo", base="main", message="Add feature",
                user="octocat", maintainer_can_modify=False):
        self.github.pulls[number] = {"title": "Existing feature", "body": "Human description", "state": "open",
                                     "merged": False, "draft": True, "user": user, "branch": branch,
                                     "head_repo": head_repo, "base": base,
                                     "maintainer_can_modify": maintainer_can_modify}
        # The branch name keeps heads of different PRs distinct, so no PR shares another's rejected commit.
        return self.commit(branch, base, "feature.txt", f"external feature {branch}\n", message)

    def push_external(self, branch="feature", message="External change"):
        return self.commit(branch, branch, "external.txt", "human follow-up\n", message)

    def advance_base(self):
        return self.commit("main", "main", "base.txt", "Updated base\n", "Update base")

    def local_commit(self, run, message="Local change"):
        cwd = self.store.workspace(run)
        (cwd / "local.txt").write_text("operator change\n")
        git(cwd, "add", ".")
        git(cwd, *COMMIT, "commit", "-m", message)
        return git(cwd, "rev-parse", "HEAD")

    def adopt_pr(self, mode="review", number="7", contributors=("human",), **options):
        return self.team.adopt_pr("demo", number, mode, list(contributors), **options)

    def select(self, run, operations, grants=(), **options):
        return self.team.select("demo", list(operations), list(grants), run_id=run["id"], **options)

    def update(self, run):
        return self.team.update_pr(run["id"], ["human"])

    def report(self, run):
        return self.team.pr_report(run["id"])

    def refuses(self, message, action, *args, **kwargs):
        with self.assertRaisesRegex(TeamError, message):
            action(*args, **kwargs)

    def writable(self, mode="revise", **options):
        """Adopt PR #7 with push access to its repository and every adoption grant."""
        self.github.permissions["example/demo"] = True
        return self.adopt_pr(mode, grants=ALL, **options)

    def reviewed(self, number="7", count=3, contributors=("human",), **options):
        return self.ticks(self.adopt_pr("review", number, contributors, **options), count)

    def ticks(self, run, count):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def tick_raising(self, run, count=1):
        """Ticks that may raise; returns the stored run."""
        try:
            return self.ticks(run, count)
        except TeamError:
            return self.store.get(run["id"])

    def remote_head(self, branch="feature"):
        return git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def roles(self):
        return [role for _, role in self.agents.calls]

    def until_handoff(self, run):
        for _ in range(10):
            if run["stage"] == "handoff":
                break
            run = self.ticks(run, 1)
        return run

    def tick_moving_during_review(self, run):
        real = self.agents.run

        def moving(agent, role, *args, **kwargs):
            if role == "review":
                self.push_external()
            return real(agent, role, *args, **kwargs)

        with patch.object(self.agents, "run", side_effect=moving):
            return self.ticks(run, 1)

    def writes(self, method, when, action):
        """Patch a GitHub write so `action` runs right after each real write whose marker or state matches."""
        real = getattr(self.github, method)

        def wrapped(repo, target, key, *args, **kwargs):
            result = real(repo, target, key, *args, **kwargs)
            if when(key):
                action()
            return result

        return patch.object(self.github, method, side_effect=wrapped)

    def assert_untouched(self, head, number=7, branch="feature"):
        """No push, replacement PR, or draft change."""
        self.assertEqual((self.remote_head(branch), self.github.creates), (head, 0))
        self.assertTrue(self.github.pulls[number]["draft"])

    def assert_historical(self, report):
        self.assertFalse(report["current_evidence"] or report["independent_review_success"]
                         or report["validation"]["passed_for_candidate"])

    def assert_review(self, review, commit, current, verdict="pass", base=None, agent=None):
        self.assertEqual((review["commit"], review["current"], review["verdict"]), (commit, current, verdict))
        if base:
            self.assertEqual(review["base"], base)
        if agent:
            self.assertEqual(review["reviewer"]["agent"], agent)

    def assert_withheld(self, run, head):
        self.assertEqual((run["stage"], run.get("reviewed_sha"), run["review_withheld"]["sha"]), ("stopped", None, head))

    def test_review_only_reports_locally_without_edits_or_github_writes(self):
        head = self.open_pr(user="claude-bot")
        base = git(self.remote, "rev-parse", "main")
        run = self.adopt_pr()
        info = run["adopted_pr"]
        self.assertEqual((run["pr"], run["sha"], run["base_sha"], run["issue"]), (7, head, base, None))
        self.assertEqual((info["head_repo"], info["head_ref"], info["base_ref"], info["state"], info["draft"],
                          info["mode"]), ("example/demo", "feature", "main", "open", True, "review"))
        # A GitHub username never implies a model family.
        self.assertEqual((info["github_identities"]["pr_author"], run["contributors"]), ("claude-bot", ["human"]))
        self.assertTrue(run["independence"]["established"])
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
        self.assert_untouched(head)
        self.assertEqual((self.github.comments, self.github.statuses), ({}, []))
        report = self.report(run)
        self.assertEqual((report["ci_checks"], report["roles"]["reviser"], report["local_handoff"]),
                         ("not checked", None, None))
        self.assertTrue(report["independent_review_success"] and report["validation"]["passed_for_candidate"])
        self.assertEqual(report["currency"], {"verified": True, "reason": None})
        self.assertIn("GitHub CI checks were not checked.", report["limitations"])
        self.assertTrue(any("Readiness was not assessed" in item for item in report["limitations"]))
        self.assertEqual(self.ticks(run, 2)["stage"], "stopped")
        self.assertEqual(len(self.agents.calls), 1)

    def test_review_only_rejection_reports_findings_and_stops(self):
        for grants in self.scenarios(["github"], []):
            head = self.open_pr()
            self.agents.reject, self.agents.summary = True, "Feature text is wrong"
            run = self.ticks(self.adopt_pr(number="https://github.com/example/demo/pull/7", grants=grants), 3)
            self.assertEqual((run["stage"], run["next_stage"], run["review_record"]), ("stopped", "implement", None))
            self.assertIn(head, run["rejected_shas"])
            if grants:
                body = self.github.comments[(7, f"{run['id']}-review-0-{head}")]
                for text in ("changes requested", "GitHub CI checks: not checked by this review.",
                             "not a readiness verdict"):
                    self.assertIn(text, body)
                self.assertIn((head, "failure"), self.github.statuses)
            else:
                self.assertEqual((self.github.comments, self.github.statuses), ({}, []))
            # The stop boundary holds: no repair loop, edit, push, or readiness change.
            run = self.ticks(run, 3)
            self.assertEqual((run["stage"], self.roles()), ("stopped", ["review"]))
            self.assert_untouched(head)
            review = self.report(run)["review"]
            self.assert_review(review, head, True, "changes_requested", agent=run["reviewer"])
            self.assertEqual((review["summary"], review["findings"][0]["location"], review["candidate_verdict"],
                              review["validation_failed"]),
                             ("Feature text is wrong", "feature.txt:1", "changes_requested", False))

    def test_adoption_refuses_unauthorized_or_inconsistent_requests(self):
        self.open_pr()
        for mode, grants, options, message in (
                ("review", ["edit"], {}, "Review-only never edits"),
                ("review", ["push"], {}, "Review-only never edits"),
                ("revise", [], {}, "requires --grant edit"),
                ("review", ["readiness"], {}, "never changes PR readiness"),
                ("findings", ["edit"], {}, "--finding"),
                ("review", [], {"findings": ["Fix it"]}, "--finding")):
            with self.subTest(mode=mode, grants=grants):
                self.refuses(message, self.adopt_pr, mode, grants=grants, **options)
        self.refuses("Declare every contributor", self.adopt_pr, contributors=())
        self.refuses("not registered repository", self.adopt_pr, number="https://github.com/other/demo/pull/7")
        self.assertEqual(pull_number(self.project, "https://github.com/Example/Demo/pull/7/files"), 7)
        self.assertEqual((self.store.runs(), self.agents.calls), ([], []))

    def test_review_and_revise_pushes_fast_forward_to_existing_branch(self):
        head = self.open_pr()
        self.agents.reject = 1
        run = self.writable()
        self.assertEqual((run["operations"], run["pr_followup"]),
                         (["validate", "review"], ["revision", "validate", "publish", "review"]))
        run = self.ticks(run, 7)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review"), (run["author"], "implement"),
                                             (run["reviewer"], "review")])
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((self.remote_head(), git(self.remote, "rev-parse", f"{run['sha']}^")), (run["sha"], head))
        self.assertEqual((run["reviewed_sha"], run["adopted_pr"]["pushed"]), (run["sha"], [run["sha"]]))
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual((self.github.creates, self.github.pulls[7]["body"]), (0, "Human description"))
        self.assertTrue(self.github.pulls[7]["draft"])
        self.assertIn((run["sha"], "pending", "Revision pushed; independent review pending"),
                      self.github.status_descriptions)
        self.assertNotIn("success", [state for _, state in self.github.statuses])
        self.assertIn((7, f"{run['id']}-review-1-{run['sha']}"), self.github.comments)

    def test_fork_review_and_missing_write_permission_hand_off_locally(self):
        head = self.open_pr(8, "fork-feature", head_repo="someone/demo-fork")
        plan = self.adopt_pr("revise", "8", grants=ALL, plan_only=True)
        self.assertFalse(plan["push"]["allowed"])
        self.assertIn("someone/demo-fork", plan["push"]["reason"])
        self.assertEqual(self.store.runs(), [])
        self.agents.reject = 1
        run = self.adopt_pr("revise", "8", grants=ALL)
        self.assertEqual(run["adopted_pr"]["head_repo"], "someone/demo-fork")
        self.assertNotIn("publish", run["pr_followup"])
        run = self.ticks(run, 6)
        self.assertEqual(run["stage"], "stopped")
        self.assert_untouched(head, 8, "fork-feature")
        handoff = self.report(run)["local_handoff"]
        self.assertEqual((handoff["commit"], handoff["builds_on"], handoff["replacement_pr"]),
                         (run["sha"], head, "not created"))
        self.assertIn("someone/demo-fork", handoff["reason"])
        self.assertIn("fixed", Path(handoff["patch"]).read_text())
        # Evidence for the unpublished commit never makes the unchanged PR ready, on selection or recovery.
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (run["sha"], run["sha"]))
        self.refuses("published PR head", self.select, run, ["ci"], ["github", "readiness"])
        self.store.save(run, stage="ci", grants=["github", "readiness"], operations=["ci"], stop_after="ci")
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("published PR head", run["error"])
        self.assertNotIn("success", [state for _, state in self.github.statuses])
        self.assertTrue(self.github.pulls[8]["draft"])

    def test_fork_with_maintainer_edits_can_push(self):
        self.open_pr(8, "fork-feature", head_repo="someone/demo-fork", maintainer_can_modify=True)
        self.github.permissions["example/demo"] = True
        plan = self.adopt_pr("revise", "8", grants=ALL, plan_only=True)
        self.assertTrue(plan["push"]["allowed"])
        self.assertIn("maintainer edits", plan["push"]["reason"])
        self.assertIn("publish", plan["revision_operations"])
        self.refuses("github", self.adopt_pr, "revise", "8", grants=["edit", "push"])

    def test_unknown_mixed_or_unsupported_authorship_withholds_independent_success(self):
        for trailer, declared, refusal, contributors, unresolved in self.scenarios(
                (None, ["human", "unknown"], "unknown contributors", ["human", "unknown"], []),
                ("anthropic", ["openai"], "both model families", ["anthropic", "openai"], []),
                ("unknown", ["human"], "unknown or unsupported model families", ["human"], ["unknown"]),
                ("gemini", ["human"], "unknown or unsupported model families", ["human"], ["gemini"])):
            head = self.open_pr(message="Add feature" + (f"\n\nAgent-Family: {trailer}" if trailer else ""),
                                user="openai-codex")
            self.refuses(refusal, self.adopt_pr, "revise", contributors=declared, grants=["edit"])
            run = self.adopt_pr(contributors=declared, grants=["github"])
            # Trailer values are never recorded as contributors, and usernames never imply a family.
            self.assertEqual((run["contributors"], run["adopted_pr"]["unresolved_trailers"]), (contributors, unresolved))
            self.assertEqual(run["adopted_pr"]["trailer_families"], ["anthropic"] if trailer == "anthropic" else [])
            self.assertFalse(run["independence"]["established"])
            self.assertIn(refusal, run["independence"]["reason"])
            run = self.ticks(run, 3)
            self.assert_withheld(run, head)
            body = self.github.comments[(7, f"{run['id']}-review-0-{head}")]
            self.assertIn("Review (independence not established)", body)
            self.assertIn(f"Independent-review success withheld: {run['independence']['reason']}", body)
            self.assertNotIn("success", [state for _, state in self.github.statuses])
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["authorship"]["unresolved_trailers"], unresolved)
            self.refuses("exact-commit independent review", self.select, run, ["ci"], ["readiness"])

    def test_single_family_authorship_assigns_independent_roles(self):
        self.open_pr(message="Add feature\n\nAgent-Family: openai")
        self.refuses("cannot review independently", self.adopt_pr, reviewer="codex")
        run = self.adopt_pr()
        self.assertEqual((run["author"], run["reviewer"], run["contributors"]), ("codex", "claude", ["human", "openai"]))
        self.store.save(run, stage="closed")
        # A known family alongside an unsupported value still never reviews its own work.
        self.open_pr(9, "branch-9", message="Add feature\n\nAgent-Family: openai\nAgent-Family: gemini")
        run = self.adopt_pr(number="9")
        self.assertEqual((run["author"], run["reviewer"], run["adopted_pr"]["trailer_families"]),
                         ("codex", "claude", ["openai"]))
        self.assertFalse(run["independence"]["established"])

    def test_duplicate_adoption_and_conflicting_ownership_are_refused(self):
        self.open_pr()
        run = self.adopt_pr()
        self.refuses(f"already tracked by run {run['id']}", self.adopt_pr, "revise", grants=["edit"])
        self.store.save(run, stage="closed")
        self.refuses("already has a run", self.adopt_pr)
        owned = self.store.create(self.project, self.github.items[0])
        self.store.save(owned, pr=9, stage="stopped")
        self.refuses(f"already tracked by run {owned['id']}", self.adopt_pr, number="9")
        self.assertEqual(len(self.store.runs()), 2)

    def test_readoption_after_stopped_exhausted_run_keeps_budget(self):
        self.store.update_project("demo", max_revisions=1)
        self.open_pr()
        # The existing head and its revision are both rejected (`True` counts as one rejection).
        self.agents.reject = 2
        run = self.until_handoff(self.adopt_pr("revise", grants=["edit"]))
        self.assertEqual((run["stage"], run["round"]), ("handoff", 1))
        self.assertEqual(self.team.decide(run["id"], "stop")["stage"], "closed")
        # An external commit changes the adoption inputs; the exhausted budget still applies.
        external = self.push_external()
        self.store.update_project("demo", max_revisions=5)
        calls = list(self.agents.calls)
        self.refuses(f"reached its revision limit in run {run['id']}", self.adopt_pr, "revise", grants=["edit"])
        self.refuses("reached its revision limit", self.adopt_pr, "findings", grants=["edit"], findings=["Fix it"])
        self.assertEqual(len(self.store.runs()), 1)
        review = self.adopt_pr()
        self.assertEqual((review["sha"], review["round"], review["revision_limit"]), (external, 1, 1))
        prior = review["prior_runs"][0]
        self.assertEqual((prior["id"], prior["handoffs"], prior["decisions"]), (run["id"], 1, ["stop"]))
        self.assertEqual(set(review["rejected_shas"]), set(run["rejected_shas"]))
        self.assertEqual(self.agents.calls, calls)

    def test_readoption_after_closed_run_continues_its_budget(self):
        self.store.update_project("demo", max_revisions=2)
        self.open_pr()
        self.agents.reject = True
        run = self.reviewed()
        self.assertEqual((run["stage"], run["round"]), ("stopped", 1))
        self.store.save(run, stage="closed")
        self.push_external()
        plan = self.adopt_pr("revise", grants=["edit"], plan_only=True)
        self.assertEqual(plan["revision_budget"], {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        plan = self.adopt_pr("findings", grants=["edit"], findings=["Fix it"], plan_only=True)
        self.assertEqual(plan["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        self.refuses("no revision budget left", self.adopt_pr, "findings", grants=["edit"], findings=["Fix it"])
        revised = self.adopt_pr("revise", grants=["edit"])
        self.assertEqual((revised["round"], revised["revision_limit"]), (2, 2))

    def test_closed_merged_and_incompatible_base_are_handled_before_mutation(self):
        self.open_pr()
        self.github.pulls[7]["state"] = "closed"
        self.refuses("never reopens", self.adopt_pr)
        self.github.pulls[7]["merged"] = True
        self.refuses("merged", self.adopt_pr)
        self.assertEqual(self.github.pulls[7]["state"], "closed")
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(8, "release-fix", base="release")
        self.refuses("never retargets", self.adopt_pr, "revise", "8", grants=["edit"])
        run = self.adopt_pr(number="8")
        self.assertEqual(run["adopted_pr"]["base_ref"], "release")
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], run["reviewed_sha"], self.github.pulls[8]["base"]), ("stopped", head, "release"))

    def test_revision_after_review_only_rejection_is_refused_before_editing(self):
        for number, branch, base, contributors, message in self.scenarios(
                (7, "feature", "main", ["human", "unknown"], "unknown contributors"),
                (8, "mixed", "main", ["openai", "anthropic"], "both model families"),
                (9, "release-fix", "release", ["human"], "never retargets")):
            git(self.remote, "branch", "release", "main")
            head = self.open_pr(number, branch, base=base)
            self.agents.reject = True
            run = self.reviewed(str(number), contributors=contributors)
            self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "implement"))
            calls = list(self.agents.calls)
            self.refuses(message, self.select, run, ["revision", "validate"], ["edit"])
            run = self.store.get(run["id"])
            self.assertEqual(run["stage"], "stopped")
            # Recovery into a revision stage is refused by the same check before any author call.
            self.store.save(run, stage="revision", grants=["edit"], operations=["revision", "validate"],
                            stop_after="validate")
            try:
                self.ticks(run, 1)
            except TeamError as error:
                self.assertRegex(str(error), message)
            run = self.store.get(run["id"])
            self.assertNotIn(run["stage"], {"validate", "review"})
            self.assertEqual(self.agents.calls, calls)
            self.assertNotIn("implement", self.roles())
            cwd = self.store.workspace(run)
            self.assertEqual((git(cwd, "rev-parse", "HEAD"), git(cwd, "status", "--porcelain")), (head, ""))
            self.assertEqual(self.remote_head(branch), head)

    def readiness_run(self):
        head = self.open_pr()
        run = self.reviewed(grants=["github"])
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        self.assertEqual(self.report(run)["ci_checks"], "not checked")
        return head, self.select(run, ["ci"], ["github", "readiness"])

    def test_readiness_records_successful_pending_and_failed_ci(self):
        for state in self.scenarios("success", "pending"):
            head, run = self.readiness_run()
            self.github.check_state = state
            run = self.ticks(run, 1 if state == "success" else 3)
            report = self.report(run)
            check = report["current_ci"]
            self.assertEqual((check["operation"], check["head"], check["base"], check["state"]),
                             ("ci", head, run["base_sha"], state))
            self.assertEqual(report["ci_checks"], [check])
            ready = state == "success"
            self.assertEqual((run["stage"], check["readiness_changed"], self.github.pulls[7]["draft"]),
                             ("ready" if ready else "ci", ready, not ready))
            if ready:
                self.assertFalse(any("CI checks" in item for item in report["limitations"]))
                continue
            self.assertIn("GitHub CI checks for the current candidate did not pass (state: pending).",
                          report["limitations"])
            self.github.check_state = "failure"
            run = self.tick_raising(run)
            self.assertNotEqual(run["stage"], "ready")
            self.assertTrue(self.github.pulls[7]["draft"])
            report = self.report(run)
            self.assertEqual(([c["state"] for c in report["ci_checks"]], report["current_ci"]["head"]),
                             (["pending", "failure"], head))
            self.assertIn("GitHub CI checks for the current candidate did not pass (state: failure).",
                          report["limitations"])

    def test_ci_observations_are_invalidated_by_movement(self):
        head, run = self.readiness_run()
        self.github.check_state = "pending"
        self.assertEqual(self.ticks(run, 1)["stage"], "ci")
        self.advance_base()
        self.assertEqual(self.tick_raising(run)["stage"], "stale")
        report = self.report(run)
        self.assertIsNone(report["current_ci"])
        self.assertEqual((report["ci_checks"][0]["head"], report["ci_checks"][0]["current"]), (head, False))
        self.assertIn(HISTORICAL_CI, report["limitations"])

    def test_ci_observations_stay_historical_after_invalidation_and_fresh_validation(self):
        for change in self.scenarios("configuration", "pins"):
            head = self.open_pr()
            run = self.ticks(self.select(self.reviewed(grants=["github"]), ["checks"]), 1)
            check = self.report(run)["current_ci"]
            self.assertEqual((check["operation"], check["state"], check["head"]), ("checks", "success", head))
            if change == "configuration":
                self.store.update_project("demo", tests=["true"])
            else:
                self.team.invalidate_pins(self.store.project("demo"), run)
            # New validation and review renew the evidence, but not the earlier CI observation.
            run = self.ticks(self.select(run, ["validate", "review"]), 2)
            report = self.report(run)
            self.assertEqual((run["stage"], run["reviewed_sha"], report["current_evidence"]), ("stopped", head, True))
            self.assertIsNone(report["current_ci"])
            self.assertFalse(report["ci_checks"][0]["current"])
            self.assertIn(HISTORICAL_CI, report["limitations"])

    def test_movement_during_readiness_writes_stops_before_ready(self):
        for write, move in self.scenarios(("comment", "push_external"), ("status", "advance_base")):
            head, run = self.readiness_run()
            with self.writes(write, lambda key: "-review-" in key or key == "success", getattr(self, move)):
                run = self.ticks(run, 1)
            self.assertEqual((run["stage"], run["validated_sha"], run["reviewed_sha"]), ("stale", None, None))
            self.assertTrue(self.github.pulls[7]["draft"])
            self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
            self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
            self.assertFalse(run["ci_checks"][-1]["readiness_changed"])
            if write == "comment":
                self.assertNotIn((head, "success"), self.github.statuses)

    def test_movement_after_marking_ready_is_never_saved_as_ready(self):
        head, run = self.readiness_run()
        with self.writes("comment", lambda key: key.endswith("-ready"), self.push_external):
            run = self.ticks(run, 1)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stale", None))
        # The draft change already happened; the report says so instead of claiming nothing changed.
        self.assertFalse(self.github.pulls[7]["draft"])
        self.assertTrue(run["ci_checks"][-1]["readiness_changed"])
        limitations = self.report(run)["limitations"]
        self.assertIn(f"Agent Team marked the PR ready for {head}; that readiness is not current.", limitations)
        self.assertFalse(any("did not change draft" in item for item in limitations))

    def test_pr_show_verifies_the_complete_binding_without_a_tick(self):
        for change in self.scenarios("head", "base", "identity", "configuration", "pins", "local", "worker", "ready"):
            if change == "ready":
                head, run = self.readiness_run()
                run = self.ticks(run, 1)
            else:
                head = self.open_pr()
                run = self.reviewed()
            report = self.report(run)
            self.assertTrue(report["current_evidence"] and report["independent_review_success"])
            if change in {"head", "ready"}:
                self.push_external()
            elif change == "base":
                self.advance_base()
            elif change == "identity":
                git(self.remote, "branch", "other-branch", head)
                self.github.pulls[7]["branch"] = "other-branch"
            elif change == "configuration":
                self.store.update_project("demo", tests=["true"])
            elif change == "local":
                self.local_commit(run)
            busy = self.store.repository_lock("demo") if change == "worker" else nullcontext()
            with patch.object(self.team, "pins_changed", return_value=change == "pins"), busy:
                report = self.report(run)
            self.assert_historical(report)
            self.assertIsNone(report["current_ci"])
            self.assert_review(report["review"], head, False)
            self.assertIs(report["currency"]["verified"], None if change == "worker" else False)
            self.assertTrue(any(("not verified" if change == "worker" else "historical") in item
                                for item in report["limitations"]))
            # Inspection never saves; the next tick or `pr update` handles the change.
            self.assertEqual(self.store.get(run["id"]), run)

    def test_concurrent_head_change_requires_deliberate_update(self):
        self.open_pr()
        run = self.reviewed(count=2)
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("pr update", run["error"])
        self.assertEqual(self.agents.calls, [])
        self.refuses("pr update", self.team.refresh, run["id"])
        run = self.update(run)
        self.assertEqual((run["stage"], run["next_stage"], run["sha"], run["validated_sha"]),
                         ("stopped", "validate", external, None))
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir())
        self.assertTrue(run["evidence_invalidations"])
        self.refuses("unchanged", self.update, run)
        run = self.ticks(self.select(run, ["validate", "review"]), 2)
        self.assertEqual((run["stage"], run["reviewed_sha"], self.remote_head()), ("stopped", external, external))

    def test_base_movement_after_stopped_review_retires_evidence_and_is_never_merged(self):
        head = self.open_pr()
        old = git(self.remote, "rev-parse", "main")
        run = self.reviewed()
        self.assertEqual(run["reviewed_sha"], head)
        base = self.advance_base()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("never merged implicitly", run["error"])
        self.assertEqual((run["reviewed_sha"], run["review_record"]), (None, None))
        report = self.report(run)
        self.assert_historical(report)
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["head"], historical["base"], historical["verdict"]), (head, old, "pass"))
        self.assertIn("PR base changed", historical["reason"])
        self.assertEqual(len(self.agents.calls), 1)
        run = self.update(run)
        self.assertEqual((run["sha"], run["base_sha"], run["reviewed_sha"], self.remote_head()), (head, base, None, head))
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assertTrue(any("base was not merged" in item for item in self.report(run)["limitations"]))

    def test_head_moved_before_publication_is_never_overwritten(self):
        self.open_pr()
        self.agents.reject = 1
        run = self.ticks(self.writable(), 5)
        self.assertEqual(run["stage"], "publish")
        local = run["sha"]
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual((run["stage"], self.remote_head(), len(self.agents.calls)), ("stale", external, 2))
        run = self.update(run)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual((update["unpushed_local_commit"], run["sha"]), (local, external))
        self.assertEqual(git(Path(update["preserved"]), "rev-parse", "HEAD"), local)

    def test_interrupted_push_reconciles_without_force(self):
        self.open_pr()
        self.agents.reject = 1
        run = self.ticks(self.writable(), 5)
        self.assertEqual(run["stage"], "publish")

        def lost_response(cwd, *args):
            result = self.local_git(cwd, *args)
            if "push" in args:
                self.assertFalse(any(str(a).startswith("+") or a == "--force" for a in args))
                raise TeamError("Simulated lost push response")
            return result

        with patch("agent_team.coordinator.git", side_effect=lost_response):
            run = self.ticks(run, 1)
        self.assertEqual((run["stage"], run["pending_push_sha"]), ("blocked", run["sha"]))
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assertEqual((run["stage"], run["reviewed_sha"], self.remote_head()), ("stopped", run["sha"], run["sha"]))
        self.assertIsNone(run["pending_push_sha"])

    def test_queued_review_comment_is_retried_or_withheld_after_changes(self):
        reasons = {"configuration": "Validation configuration changed", "pins": "Companion pins changed"}
        for change in self.scenarios(None, "base", "configuration", "pins"):
            head = self.open_pr()
            run = self.reviewed(count=2, grants=["github"])
            with patch.object(self.github, "comment", side_effect=TeamError("GitHub unavailable")), \
                    self.assertRaises(TeamError):
                self.ticks(run, 1)
            run = self.store.get(run["id"])
            self.assertEqual((run["stage"], len(run["outbox"])), ("stopped", 1))
            if change == "base":
                self.advance_base()
            elif change == "configuration":
                self.store.update_project("demo", tests=["true"])
            with patch.object(self.team, "pins_changed", return_value=change == "pins"):
                run = self.ticks(run, 1)
            self.assertEqual((run["outbox"], len(self.agents.calls)), ([], 1))
            marker = (7, f"{run['id']}-review-0-{head}")
            if change is None:
                self.assertIn(marker, self.github.comments)
                continue
            self.assertNotIn(marker, self.github.comments)
            self.assertEqual(run["unpublished_evidence"][0]["evidence"], head)
            if change == "base":
                self.assertEqual(run["stage"], "stale")
                self.assertIn("PR base changed", run["error"])
                continue
            self.assertEqual((run["stage"], run["next_stage"], self.github.comments), ("stopped", "validate", {}))
            self.assertEqual(run["unpublished_evidence"][0]["withheld_reason"], reasons[change])
            report = self.report(run)
            self.assert_historical(report)
            self.assert_review(report["review"], head, False)
            self.assertEqual(report["historical_evidence"][-1]["reason"], reasons[change])
            # The saved review is never rerun: new validation must be selected explicitly first.
            self.refuses("validat", self.select, run, ["review"])
            self.assertEqual(len(self.agents.calls), 1)

    def test_head_movement_during_review_keeps_evidence_local(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.tick_moving_during_review(self.reviewed(count=2, grants=["github"]))
        self.assertEqual(run["stage"], "stale")
        for text in ("PR head moved", "before review evidence was published"):
            self.assertIn(text, run["error"])
        self.assertNotIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual(run["outbox"], [])
        self.assertEqual({i["evidence"] for i in run["unpublished_evidence"]}, {head})
        report = self.report(run)
        self.assertEqual(len(report["unpublished_evidence"]), 2)
        self.assertTrue(any("was not published" in item for item in report["limitations"]))
        run = self.ticks(run, 2)
        self.assertEqual((run["stage"], self.github.comments, len(self.agents.calls)), ("stale", {}, 1))
        run = self.update(run)
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "validate"))

    def test_local_review_movement_retires_evidence(self):
        head = self.open_pr()
        base = git(self.remote, "rev-parse", "main")
        # Without a github grant nothing is queued for publication, yet the PR is still rechecked.
        run = self.tick_moving_during_review(self.reviewed(count=2))
        self.assertEqual((run["stage"], run["validated_sha"], run["reviewed_sha"]), ("stale", None, None))
        self.assertIn("PR head moved", run["error"])
        self.assertEqual((self.github.comments, self.github.statuses), ({}, []))
        report = self.report(run)
        self.assert_historical(report)
        self.assertEqual(report["validation"]["results"], [])
        self.assertTrue(any("historical" in item for item in report["limitations"]))
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["head"], historical["base"], historical["verdict"], historical["validated"],
                          historical["reviewed"]), (head, base, "pass", head, head))
        self.assertTrue(historical["independent_review_success"])
        # The full review stays readable, named with the commits it was gathered for.
        for review in (historical["review"], report["review"]):
            self.assert_review(review, head, False, base=base, agent=run["reviewer"])
            self.assertTrue(review["summary"])
            self.assertIn("findings", review)

    def test_deliberate_update_snapshots_review(self):
        head = self.open_pr()
        base = git(self.remote, "rev-parse", "main")
        run = self.reviewed()
        external = self.push_external()
        # Update directly from the stopped run, before any tick notices the movement.
        run = self.update(run)
        self.assertEqual((run["sha"], run["review_record"]), (external, None))
        report = self.report(run)
        historical = report["historical_evidence"][-1]
        self.assertIn("Deliberate adoption", historical["reason"])
        self.assertEqual((historical["head"], historical["base"], historical["reviewed"]), (head, base, head))
        self.assert_review(historical["review"], head, False, agent=run["reviewer"])
        self.assert_review(report["review"], head, False)
        self.assertFalse(report["independent_review_success"])

    def test_local_continuation_and_validation_drift_keep_complete_prior_evidence(self):
        head = self.open_pr()
        run = self.reviewed()
        local = self.local_commit(run)
        run = self.select(run, ["validate", "review"], contributors=["human"])
        self.assert_review(self.report(run)["review"], head, False, agent=run["reviewer"])
        run = self.ticks(run, 2)
        report = self.report(run)
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["reason"], historical["validated"], historical["reviewed"]),
                         ("Candidate or configuration changed", head, head))
        self.assert_review(historical["review"], head, False, base=run["base_sha"], agent=run["reviewer"])
        self.assertTrue(historical["review"]["summary"])
        self.assert_review(report["review"], local, True)
        self.assertTrue(report["independent_review_success"])
        self.store.save(run, stage="closed")
        # An edit between validation and review voids the validation, which is kept as history.
        head = self.open_pr(8, "drift")
        run = self.reviewed("8", count=2)
        (self.store.workspace(run) / "stray.txt").write_text("operator edit\n")
        run = self.ticks(run, 1)
        self.assertEqual((run["stage"], run["next_stage"], run["validated_sha"]), ("stopped", "validate", None))
        report = self.report(run)
        self.assert_historical(report)
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["reason"], historical["validated"], historical["head"]),
                         ("Candidate or configuration changed", head, head))
        self.assertEqual(self.roles(), ["review", "review"])

    def test_same_sha_head_identity_change_retires_evidence(self):
        for change in self.scenarios({"branch": "other-branch"}, {"head_repo": "someone/demo-fork"}):
            head = self.open_pr()
            run = self.reviewed(grants=["github"])
            self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
            if "branch" in change:
                git(self.remote, "branch", change["branch"], head)
            self.github.pulls[7].update(change)
            run = self.ticks(run, 1)
            self.assertEqual((run["stage"], run["validated_sha"], run["reviewed_sha"]), ("stale", None, None))
            self.assertIn("head repository or branch changed", run["error"])
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
            self.assertIn((head, "pending", "PR head changed; review invalidated"), self.github.status_descriptions)
            self.refuses("adopt the PR again", self.update, run)

    def test_continuation_after_update_stops_on_rejection_without_revision(self):
        head = self.open_pr()
        run = self.ticks(self.writable(), 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        external = self.push_external()
        self.assertEqual(self.ticks(run, 1)["stage"], "stale")
        self.update(run)
        run = self.select(run, ["validate", "review"])
        self.assertIsNone(run["pr_followup"])
        self.assertEqual(run["released_pr_followup"], ["revision", "validate", "publish", "review"])
        self.agents.reject = True
        run = self.ticks(run, 4)
        # The selected review endpoint holds: no edit, push, or repair loop.
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "implement", external))
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual(self.roles(), ["review", "review"])
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (external, []))

    def review_failing_head(self, mode, grants):
        """Adopt a PR whose head fails validation; both the failure and the review of that head are reported."""
        head = self.open_pr()
        self.store.update_project("demo", tests=["grep -q fixed feature.txt"])
        self.github.permissions["example/demo"] = True
        run = self.ticks(self.adopt_pr(mode, grants=grants), 2)
        # Failed validation is recorded, but the existing head is reviewed before any edit.
        self.assertEqual((run["stage"], run["tests"][0]["exit_code"], run["validated_sha"]), ("review", 1, None))
        self.assertEqual((run.get("rejected_shas", []), self.agents.calls), ([], []))
        run = self.ticks(run, 1)
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
        entry = run["revision_history"][0]
        self.assertEqual((entry["sha"], entry["kind"], entry["validation_failed"]), (head, "review", True))
        self.assertEqual(entry["findings"][0]["severity"], "validation")
        self.assertIn((head, "failure", "Configured validation failed"), self.github.status_descriptions)
        self.assertIn("exit 1", self.github.comments[(7, f"{run['id']}-review-0-{head}")])
        self.assertEqual(self.remote_head(), head)
        return head, run

    def test_review_and_revise_reviews_failing_head_before_editing(self):
        head, run = self.review_failing_head("revise", ALL)
        self.assertEqual(run["stage"], "revision")
        run = self.ticks(run, 4)
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review"), (run["author"], "implement"),
                                             (run["reviewer"], "review")])
        self.assertEqual((run["stage"], run["validated_sha"], run["reviewed_sha"]), ("stopped", run["sha"], run["sha"]))
        self.assertEqual((git(self.remote, "rev-parse", f"{run['sha']}^"), self.remote_head()), (head, run["sha"]))

    def test_review_only_reviews_failing_head_and_stops(self):
        head, run = self.review_failing_head("review", ["github"])
        # The stop boundary holds.
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "implement"))
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], self.roles()), ("stopped", ["review"]))
        self.assert_untouched(head)
        report = self.report(run)
        review = report["review"]
        # The reviewer's own pass is reported as given; the candidate is rejected for failed validation.
        self.assert_review(review, head, True)
        self.assertEqual((review["summary"], review["findings"], review["validation_failed"]),
                         (self.agents.summary, [], True))
        self.assertEqual((review["candidate_verdict"], review["candidate_findings"][0]["severity"]),
                         ("changes_requested", "validation"))
        self.assertFalse(report["validation"]["passed_for_candidate"] or report["independent_review_success"])

    def test_unresolved_trailers_in_external_repair_withhold_independence(self):
        # A declared `unknown` repair contributor withholds independence like an unresolved trailer.
        for value, declared in self.scenarios(("unknown", ["human"]), ("gemini", ["human"]), (None, ["unknown"])):
            run = self.repair_handoff()
            self.assertTrue(run["independence"]["established"])
            external = self.push_external(message=f"Repair\n\nAgent-Family: {value}" if value else "Repair")
            run = self.team.adopt(run["id"], declared)
            self.assertEqual(run["sha"], external)
            self.assertFalse(run["independence"]["established"])
            self.assertIn(value or "unknown contributors", run["independence"]["reason"])
            unresolved = [value] if value else []
            self.assertEqual((run["adopted_pr"]["unresolved_trailers"], run["adoptions"][-1]["unresolved_trailers"]),
                             (unresolved, unresolved))
            # Trailer values are never recorded as contributors; declarations are.
            self.assertEqual(run["contributors"], sorted({"human", *declared}))
            # Revision is refused before any author call; review still reports findings only.
            self.refuses("Independent review cannot be established", self.select, run, ["revision", "validate"], ["edit"])
            self.agents.reject = False
            run = self.ticks(self.select(run, ["validate", "review"], contributors=declared), 2)
            self.assert_withheld(run, external)
            self.assertNotIn("implement", self.roles())
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["authorship"]["unresolved_trailers"], unresolved)

    def test_unresolved_trailers_in_local_continuation_withhold_independence(self):
        head = self.open_pr()
        run = self.ticks(self.adopt_pr("revise", grants=["edit"]), 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        local = self.local_commit(run, "Local change\n\nAgent-Family: gemini")
        run = self.select(run, ["validate", "review"], contributors=["human"])
        self.assertFalse(run["independence"]["established"])
        self.assertEqual((run["unresolved_trailers"], run["adopted_pr"]["unresolved_trailers"]), (["gemini"], ["gemini"]))
        self.assertIsNone(run["reviewed_sha"])
        run = self.ticks(run, 2)
        self.assert_withheld(run, local)
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(self.report(run)["independent_review_success"])

    def test_movement_during_first_outbox_write_withholds_later_evidence(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.reviewed(count=2, grants=["github"])
        with self.writes("comment", lambda key: "-review-" in key, self.push_external):
            run = self.ticks(run, 1)
        # The comment went out before the move; the status that followed it did not.
        self.assertIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual((run["stage"], run["outbox"], len(self.agents.calls)), ("stale", [], 1))
        self.assertIn("PR head moved", run["error"])
        self.assertEqual([i["type"] for i in run["unpublished_evidence"]], ["status"])
        self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
        self.assertIn("PR head moved", run["evidence_invalidations"][-1]["reason"])

    def interrupt(self, point, action):
        """Run `action`, interrupting its journaled checkout swap at `point`."""
        class Interrupted(Exception):
            pass

        real_rename, real_save, renames = Path.rename, self.store.save, []

        def rename(path, target):
            result = real_rename(path, target)
            renames.append(target)
            if point == f"rename-{len(renames)}":
                raise Interrupted()
            return result

        def save(record, **changes):
            if point == "final-save" and "pending_swap" in changes and changes["pending_swap"] is None:
                raise Interrupted()
            real_save(record, **changes)
            if point == "journal" and changes.get("pending_swap"):
                raise Interrupted()

        with patch("pathlib.Path.rename", rename), patch.object(self.store, "save", side_effect=save), \
                self.assertRaises(Interrupted):
            action()

    def test_interrupted_update_recovers(self):
        for point in self.scenarios("journal", "rename-1", "rename-2", "final-save"):
            head = self.open_pr()
            run = self.reviewed()
            external = self.push_external()
            self.assertEqual(self.ticks(run, 1)["stage"], "stale")
            self.interrupt(point, lambda: self.update(run))
            run = self.store.get(run["id"])
            self.assertEqual((run["stage"], run["sha"], run["pending_swap"]["command"]),
                             ("stale", head, "pr update RUN_ID"))
            self.refuses("interrupted", self.select, run, ["validate"])
            self.refuses("interrupted", self.team.resume, run["id"])
            run = self.update(run)
            self.assertIsNone(run["pending_swap"])
            self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
            self.assertEqual(git(self.store.workspace(run), "rev-parse", "HEAD"), external)
            self.assertEqual(git(Path(run["adopted_pr"]["updates"][0]["preserved"]), "rev-parse", "HEAD"), head)
            self.assertEqual((run["validated_sha"], run["reviewed_sha"], run["evidence_context"]["head"]),
                             (None, None, external))
            self.assertEqual(run["evidence_invalidations"][-1]["reason"], "Deliberate adoption of changed PR head or base")
            self.assertEqual(self.remote_head(), external)
            run = self.ticks(self.select(run, ["validate", "review"]), 2)
            self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", external))

    def test_supplied_findings_are_revised_with_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=["Append a closing line to feature.txt"])
        self.assertEqual(run["operations"], ["revision", "validate", "publish", "review"])
        run = self.ticks(run, 5)
        self.assertEqual(run["stage"], "stopped")
        for text in ("Append a closing line to feature.txt", "existing PR #7"):
            self.assertIn(text, self.agents.prompts["implement"])
        self.assertEqual(run.get("rejected_shas", []), [])
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"], self.remote_head()), (run["sha"],) * 3)
        self.assertTrue(self.github.pulls[7]["draft"])

    def test_findings_mode_counts_its_first_edit_against_the_budget(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.refuses("no revision budget left", self.adopt_pr, "findings", grants=["edit"], findings=["Fix it"])
        self.assertEqual((self.store.runs(), self.agents.calls), ([], []))
        self.store.update_project("demo", max_revisions=1)
        self.agents.reject = True
        run = self.adopt_pr("findings", grants=["edit"], findings=["Fix it"])
        self.assertEqual((run["round"], run["revision_limit"]), (1, 1))
        run = self.until_handoff(run)
        # Like revise mode with the same limit, exactly one revision is made before the handoff.
        self.assertEqual((run["stage"], run["round"], self.roles().count("implement")), ("handoff", 1, 1))
        # The shared pre-edit guard also refuses a continuation past the budget, before any author call.
        self.store.save(run, stage="stopped", next_stage="revision", round=2, needs_revision=True)
        self.refuses("no revision budget left", self.select, run, ["revision", "validate"], ["edit"])
        self.assertEqual(self.roles().count("implement"), 1)

    def repair_handoff(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.agents.reject = True
        return self.team.decide(self.reviewed()["id"], "repair")

    def test_repair_adoption_refuses_changed_head_identity(self):
        for change in self.scenarios("repository", "branch"):
            run = self.repair_handoff()
            before = git(self.store.workspace(run), "rev-parse", "HEAD")
            if change == "repository":
                # Same commit, but the PR head now names another repository.
                self.github.pulls[7]["head_repo"] = "someone/demo"
            else:
                # Same commit, pushed to another branch that the PR now uses.
                git(self.remote, "branch", "moved", "feature")
                self.github.pulls[7]["branch"] = "moved"
            self.refuses("head repository or branch changed", self.team.adopt, run["id"], ["human"])
            after = self.store.get(run["id"])
            self.assertEqual((after["stage"], after.get("adoptions"), after["adopted_pr"]),
                             ("repair", None, run["adopted_pr"]))
            self.assertEqual(git(self.store.workspace(after), "rev-parse", "HEAD"), before)
            self.assertEqual(list(self.store.run_root(after).glob("refresh-*")), [])

    def test_evidence_is_invalidated_by_configuration_change(self):
        self.open_pr()
        run = self.reviewed()
        self.store.update_project("demo", tests=["true"])
        self.refuses("evidence invalidated", self.select, run, ["review"])
        run = self.store.get(run["id"])
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))

    def test_exhausted_review_hands_off_and_repair_is_adopted_without_base_merge(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.agents.reject = True
        run = self.reviewed()
        self.assertEqual(run["stage"], "handoff")
        self.assertIn("feature.txt:1", run["handoffs"][0]["text"])
        self.assertEqual(self.team.decide(run["id"], "repair")["stage"], "repair")
        external = self.push_external()
        self.advance_base()
        run = self.team.adopt(run["id"], ["human"])
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
        self.assertEqual((run["adopted_pr"]["head_sha"], self.remote_head()), (external, external))
        self.assertFalse(run["adopted_pr"]["base_contained"])

    def test_every_author_entry_needs_a_reserved_unused_round(self):
        for limit in self.scenarios(0, 2):
            self.store.update_project("demo", max_revisions=limit)
            head = self.open_pr()
            run = self.ticks(self.writable(), 3)
            self.assertEqual((run["stage"], run["reviewed_sha"], run["round"]), ("stopped", head, 0))
            # A passing review reserves no round, so no continuation can start an author pass.
            for operations in (["implement", "validate", "review"], ["revision", "validate"]):
                self.refuses("No revision round is reserved", self.select, run, operations, ["edit"])
            self.store.save(run, round=limit + 1, reserved_round=limit + 1, needs_revision=True)
            self.refuses("no revision budget left", self.select, run, ["implement", "validate"], ["edit"])
            self.assertEqual((self.roles(), self.remote_head()), (["review"], head))

    def test_supplied_findings_stop_on_fresh_unrelated_findings(self):
        self.store.update_project("demo", max_revisions=3)
        self.open_pr()
        self.agents.reject = 1
        self.agents.findings = [[{"severity": "P2", "location": "other.txt:3", "evidence": "Unrelated",
                                  "request": "Rewrite other.txt"}]]
        run = self.writable("findings", findings=["Append a closing line to feature.txt"])
        self.assertIsNone(run["pr_followup"])
        run = self.ticks(run, 6)
        # The fresh finding is reported; nothing outside the supplied scope is revised automatically.
        self.assertEqual((run["stage"], run["next_stage"], run["round"]), ("stopped", "implement", 2))
        self.assertEqual(self.roles(), ["implement", "review"])
        self.assertIn("supplied findings remain the revision scope", run["partial_result"])
        report = self.report(run)
        self.assertEqual((report["requested_findings"], report["review"]["findings"][0]["location"]),
                         (["Append a closing line to feature.txt"], "other.txt:3"))
        # A round whose author pass already ran can never start another one.
        self.store.save(run, round=1, reserved_round=1)
        self.refuses("each round allows one pass", self.select, run, ["revision", "validate"], ["edit"])
        self.assertEqual(len(self.agents.calls), 2)

    def test_readoption_carries_prior_provenance_forward(self):
        for first, second, message in self.scenarios((["openai"], ["human"], "cannot review independently"),
                                                     (["human", "unknown"], ["human"], "unknown contributors"),
                                                     (["openai"], ["anthropic"], "both model families")):
            self.open_pr()
            run = self.reviewed(contributors=first)
            self.store.save(run, stage="closed")
            self.push_external()
            # New declarations of the retained work never clear the earlier run's recorded authorship.
            self.refuses(message, self.adopt_pr, "revise", contributors=second, grants=["edit"], reviewer="codex")
            again = self.adopt_pr(contributors=second)
            self.assertEqual(again["adopted_pr"]["inherited_provenance"]["runs"], [run["id"]])
            self.assertTrue(set(first) <= set(again["contributors"]))
            if first == ["openai"] and second == ["human"]:
                self.assertEqual((again["author"], again["reviewer"]), ("codex", "claude"))
            else:
                self.assertIn(message, again["independence"]["reason"])

    def test_movement_during_final_review_keeps_handoff_and_retires_evidence(self):
        self.store.update_project("demo", max_revisions=0)
        head = self.open_pr()
        self.agents.reject = True
        run = self.tick_moving_during_review(self.reviewed(count=2, grants=["github"]))
        self.assertEqual((run["stage"], len(run["handoffs"])), ("handoff", 1))
        self.assertEqual((run["outbox"], self.github.comments, self.github.statuses), ([], {}, []))
        self.assertIn(f"{run['id']}-handoff-0", [i.get("marker") for i in run["unpublished_evidence"]])
        self.assertIn("PR head moved", run["evidence_retired"]["reason"])
        report = self.report(run)
        self.assertFalse(report["current_evidence"])
        self.assert_review(report["review"], head, False, "changes_requested")
        # The handoff decision and its budget stay available.
        self.assertEqual(self.team.decide(run["id"], "repair")["stage"], "repair")

    def test_closed_or_merged_during_review_retires_evidence(self):
        for merged in self.scenarios(False, True):
            head = self.open_pr()
            run = self.reviewed(count=2, grants=["github"])
            with patch.object(self, "push_external", lambda: self.github.pulls[7].update(state="closed", merged=merged)):
                run = self.tick_moving_during_review(run)
            self.assertEqual((run["stage"], run["reviewed_sha"]), ("merged" if merged else "closed", None))
            self.assertEqual((run["outbox"], self.github.comments), ([], {}))
            report = self.report(run)
            self.assert_historical(report)
            self.assert_review(report["review"], head, False)
            self.assertEqual(report["historical_evidence"][-1]["verdict"], "pass")
            self.assertEqual(self.github.pulls[7]["state"], "closed")

    def test_interrupted_repair_adoption_recovers_recorded_inputs(self):
        for point in self.scenarios("journal", "rename-1", "rename-2", "final-save"):
            run = self.repair_handoff()
            head = run["sha"]
            external = self.push_external(message="Repair")
            self.interrupt(point, lambda: self.team.adopt(run["id"], ["human"]))
            stored = self.store.get(run["id"])
            self.assertEqual((stored["stage"], stored["sha"], stored["pending_swap"]["command"]),
                             ("repair", head, "adopt RUN_ID"))
            self.refuses("interrupted", self.team.decide, run["id"], "stop")
            # The PR moves again before recovery; recovery still installs the journaled inputs.
            later = self.commit("feature", "feature", "later.txt", "later\n", "Later change")
            run = self.team.adopt(run["id"], ["human"])
            self.assertIsNone(run["pending_swap"])
            self.assertEqual((run["stage"], run["next_stage"], run["sha"], run["round"]),
                             ("stopped", "validate", external, 1))
            self.assertEqual([(a["head"], a["declared"]) for a in run["adoptions"]], [(external, ["human"])])
            self.assertEqual((run["adopted_pr"]["head_sha"], git(self.store.workspace(run), "rev-parse", "HEAD")),
                             (external, external))
            preserved = list(self.store.run_root(run).glob("author-preserved-*"))
            self.assertEqual([git(p, "rev-parse", "HEAD") for p in preserved], [head])
            self.assertEqual(self.remote_head(), later)
            run = self.ticks(run, 1)
            self.assertEqual(run["stage"], "stale")
            self.assertIn("agent-team adopt", run["error"])

    def test_cli_parses_existing_pr_operations(self):
        args = parser().parse_args(["pr", "review", "demo", "7", "--contributor", "unknown"])
        self.assertEqual((args.pr_command, args.pull, args.contributor), ("review", "7", ["unknown"]))
        args = parser().parse_args(["pr", "findings", "demo", "https://github.com/example/demo/pull/7",
                                    "--contributor", "human", "--grant", "edit", "--finding", "Fix the parser"])
        self.assertEqual(args.finding, ["Fix the parser"])
        self.assertEqual(parser().parse_args(["pr", "update", "RUN", "--contributor", "human"]).run_id, "RUN")
        with self.assertRaises(SystemExit):
            parser().parse_args(["pr", "review", "demo", "7", "--contributor", "human", "--grant", "readiness"])
        self.assertEqual(parser().parse_args(["adopt", "RUN", "--contributor", "unknown"]).contributor, ["unknown"])
        # `unknown` is accepted only for adopted PRs; new issue or task selections still refuse it.
        self.refuses("Unknown contributor", self.team.select, "demo", ["validate"], [], task="Validate", ref="main",
                     contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
