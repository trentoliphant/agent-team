"""Adoption of existing pull requests. Local Git fixtures and fake providers only; no model or GitHub calls."""
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
        return self.commit(branch, base, "feature.txt", "external feature\n", message)

    def push_external(self, branch="feature", message="External change"):
        return self.commit(branch, branch, "external.txt", "human follow-up\n", message)

    def advance_base(self):
        return self.commit("main", "main", "base.txt", "Updated base\n", "Update base")

    def writable(self, mode="revise", **options):
        """Adopt PR #7 with push access to its repository and every adoption grant."""
        self.github.permissions["example/demo"] = True
        return self.team.adopt_pr("demo", "7", mode, ["human"], grants=["edit", "push", "github"], **options)

    def reviewed(self, number="7", count=3, contributors=("human",), **options):
        return self.ticks(self.team.adopt_pr("demo", number, "review", list(contributors), **options), count)

    def ticks(self, run, count):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def remote_head(self, branch="feature"):
        return git(self.remote, "rev-parse", f"refs/heads/{branch}")

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

    def test_review_only_reports_locally_without_edits_or_github_writes(self):
        head = self.open_pr(user="claude-bot")
        base = git(self.remote, "rev-parse", "main")
        run = self.team.adopt_pr("demo", "7", "review", ["human"])
        info = run["adopted_pr"]
        self.assertEqual((run["pr"], run["sha"], run["base_sha"], run["issue"]), (7, head, base, None))
        self.assertEqual((info["head_repo"], info["head_ref"], info["base_ref"]), ("example/demo", "feature", "main"))
        self.assertEqual((info["state"], info["draft"], info["mode"]), ("open", True, "review"))
        # A GitHub username never implies a model family.
        self.assertEqual(info["github_identities"]["pr_author"], "claude-bot")
        self.assertEqual(run["contributors"], ["human"])
        self.assertTrue(run["independence"]["established"])
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
        self.assertEqual(run["reviewed_sha"], head)
        self.assertEqual(self.remote_head(), head)
        self.assertEqual((self.github.comments, self.github.statuses, self.github.creates), ({}, [], 0))
        self.assertTrue(self.github.pulls[7]["draft"])
        report = self.team.pr_report(run["id"])
        self.assertEqual(report["ci_checks"], "not checked")
        self.assertTrue(report["independent_review_success"])
        self.assertTrue(report["validation"]["passed_for_candidate"])
        self.assertIsNone(report["local_handoff"])
        self.assertIn("GitHub CI checks were not checked.", report["limitations"])
        self.assertTrue(any("Readiness was not assessed" in item for item in report["limitations"]))
        self.assertEqual(report["roles"]["reviser"], None)
        self.assertEqual(self.ticks(run, 2)["stage"], "stopped")
        self.assertEqual(len(self.agents.calls), 1)

    def test_review_only_rejection_publishes_scoped_findings_and_stops(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.team.adopt_pr("demo", "https://github.com/example/demo/pull/7", "review", ["human"], grants=["github"])
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "implement"))
        self.assertIn(head, run["rejected_shas"])
        body = self.github.comments[(7, f"{run['id']}-review-0-{head}")]
        self.assertIn("changes requested", body)
        self.assertIn("GitHub CI checks: not checked by this review.", body)
        self.assertIn("not a readiness verdict", body)
        self.assertIn((head, "failure"), self.github.statuses)
        # The stop boundary holds: no repair loop, edit, push, or readiness change.
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual([role for _, role in self.agents.calls], ["review"])
        self.assertEqual(self.remote_head(), head)
        self.assertTrue(self.github.pulls[7]["draft"])
        self.assertEqual(self.github.creates, 0)
        report = self.team.pr_report(run["id"])
        self.assertEqual(report["review"]["verdict"], "changes_requested")
        self.assertEqual(report["review"]["summary"], self.agents.summary)
        self.assertEqual(report["review"]["findings"][0]["location"], "feature.txt:1")

    def test_local_rejection_report_keeps_full_review(self):
        head = self.open_pr()
        self.agents.reject = True
        self.agents.summary = "Feature text is wrong"
        run = self.reviewed()
        self.assertEqual((run["stage"], run["review_record"]), ("stopped", None))
        self.assertEqual(self.github.comments, {})
        review = self.team.pr_report(run["id"])["review"]
        self.assertEqual((review["commit"], review["current"]), (head, True))
        self.assertEqual((review["verdict"], review["summary"]), ("changes_requested", "Feature text is wrong"))
        self.assertEqual(review["findings"][0]["location"], "feature.txt:1")
        self.assertEqual(review["reviewer"]["agent"], run["reviewer"])
        self.assertEqual(review["candidate_verdict"], "changes_requested")
        self.assertFalse(review["validation_failed"])

    def test_adoption_refuses_unauthorized_or_inconsistent_requests(self):
        self.open_pr()
        for mode, grants, options, message in (
                ("review", ["edit"], {}, "Review-only never edits"),
                ("review", ["push"], {}, "Review-only never edits"),
                ("revise", [], {}, "requires --grant edit"),
                ("review", ["readiness"], {}, "never changes PR readiness"),
                ("findings", ["edit"], {}, "--finding"),
                ("review", [], {"findings": ["Fix it"]}, "--finding")):
            with self.subTest(mode=mode, grants=grants), self.assertRaisesRegex(TeamError, message):
                self.team.adopt_pr("demo", "7", mode, ["human"], grants=grants, **options)
        with self.assertRaisesRegex(TeamError, "Declare every contributor"):
            self.team.adopt_pr("demo", "7", "review", [])
        with self.assertRaisesRegex(TeamError, "not registered repository"):
            self.team.adopt_pr("demo", "https://github.com/other/demo/pull/7", "review", ["human"])
        self.assertEqual(pull_number(self.project, "https://github.com/Example/Demo/pull/7/files"), 7)
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.agents.calls, [])

    def test_review_and_revise_pushes_fast_forward_to_existing_branch(self):
        head = self.open_pr()
        self.agents.reject = 1
        run = self.writable()
        self.assertEqual(run["operations"], ["validate", "review"])
        self.assertEqual(run["pr_followup"], ["revision", "validate", "publish", "review"])
        run = self.ticks(run, 7)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review"), (run["author"], "implement"),
                                             (run["reviewer"], "review")])
        self.assertNotEqual(run["sha"], head)
        self.assertEqual(self.remote_head(), run["sha"])
        self.assertEqual(git(self.remote, "rev-parse", f"{run['sha']}^"), head)
        self.assertEqual(run["reviewed_sha"], run["sha"])
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(self.github.pulls[7]["body"], "Human description")
        self.assertTrue(self.github.pulls[7]["draft"])
        self.assertIn((run["sha"], "pending", "Revision pushed; independent review pending"),
                      self.github.status_descriptions)
        self.assertNotIn("success", [state for _, state in self.github.statuses])
        self.assertIn((7, f"{run['id']}-review-1-{run['sha']}"), self.github.comments)
        self.assertEqual(run["adopted_pr"]["pushed"], [run["sha"]])

    def test_fork_review_and_missing_write_permission_hand_off_locally(self):
        head = self.open_pr(8, "fork-feature", head_repo="someone/demo-fork")
        plan = self.team.adopt_pr("demo", "8", "revise", ["human"], grants=["edit", "push", "github"], plan_only=True)
        self.assertFalse(plan["push"]["allowed"])
        self.assertIn("someone/demo-fork", plan["push"]["reason"])
        self.assertEqual(self.store.runs(), [])
        self.agents.reject = 1
        run = self.team.adopt_pr("demo", "8", "revise", ["human"], grants=["edit", "push", "github"])
        self.assertEqual(run["adopted_pr"]["head_repo"], "someone/demo-fork")
        self.assertNotIn("publish", run["pr_followup"])
        run = self.ticks(run, 6)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.remote_head("fork-feature"), head)
        self.assertEqual(self.github.creates, 0)
        report = self.team.pr_report(run["id"])
        handoff = report["local_handoff"]
        self.assertEqual((handoff["commit"], handoff["builds_on"]), (run["sha"], head))
        self.assertEqual(handoff["replacement_pr"], "not created")
        self.assertIn("someone/demo-fork", handoff["reason"])
        self.assertIn("fixed", Path(handoff["patch"]).read_text())
        # Evidence for the unpublished commit never makes the unchanged PR ready, on selection or recovery.
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (run["sha"], run["sha"]))
        with self.assertRaisesRegex(TeamError, "published PR head"):
            self.team.select("demo", ["ci"], ["github", "readiness"], run_id=run["id"])
        self.store.save(run, stage="ci", grants=["github", "readiness"], operations=["ci"], stop_after="ci")
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("published PR head", run["error"])
        self.assertNotIn("success", [state for _, state in self.github.statuses])
        self.assertTrue(self.github.pulls[8]["draft"])

    def test_fork_with_maintainer_edits_can_push(self):
        self.open_pr(8, "fork-feature", head_repo="someone/demo-fork", maintainer_can_modify=True)
        self.github.permissions["example/demo"] = True
        plan = self.team.adopt_pr("demo", "8", "revise", ["human"], grants=["edit", "push", "github"], plan_only=True)
        self.assertTrue(plan["push"]["allowed"])
        self.assertIn("maintainer edits", plan["push"]["reason"])
        self.assertIn("publish", plan["revision_operations"])
        with self.assertRaisesRegex(TeamError, "github"):
            self.team.adopt_pr("demo", "8", "revise", ["human"], grants=["edit", "push"])

    def test_unknown_authorship_withholds_independent_success(self):
        head = self.open_pr()
        with self.assertRaisesRegex(TeamError, "unknown contributors"):
            self.team.adopt_pr("demo", "7", "revise", ["unknown"], grants=["edit"])
        run = self.team.adopt_pr("demo", "7", "review", ["human", "unknown"], grants=["github"])
        self.assertFalse(run["independence"]["established"])
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertIsNone(run.get("reviewed_sha"))
        self.assertEqual(run["review_withheld"]["sha"], head)
        body = self.github.comments[(7, f"{run['id']}-review-0-{head}")]
        self.assertIn("Review (independence not established)", body)
        self.assertIn("Independent-review success withheld: authorship includes unknown contributors", body)
        self.assertNotIn("success", [state for _, state in self.github.statuses])
        report = self.team.pr_report(run["id"])
        self.assertFalse(report["independent_review_success"])
        with self.assertRaisesRegex(TeamError, "exact-commit independent review"):
            self.team.select("demo", ["ci"], ["readiness"], run_id=run["id"])

    def test_mixed_family_authorship_is_recorded_and_withheld(self):
        head = self.open_pr(message="Add feature\n\nAgent-Family: anthropic", user="openai-codex")
        with self.assertRaisesRegex(TeamError, "both model families"):
            self.team.adopt_pr("demo", "7", "revise", ["openai"], grants=["edit"])
        run = self.team.adopt_pr("demo", "7", "review", ["openai"])
        self.assertEqual(run["contributors"], ["anthropic", "openai"])
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["anthropic"])
        self.assertEqual(run["independence"]["reason"], "both model families contributed")
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["review_withheld"]["sha"], head)
        self.assertIsNone(run.get("reviewed_sha"))

    def test_unknown_and_unsupported_trailers_withhold_independence(self):
        for number, value in self.scenarios((7, "unknown"), (8, "gemini")):
            head = self.open_pr(number, f"branch-{number}", message=f"Add feature\n\nAgent-Family: {value}")
            with self.assertRaisesRegex(TeamError, "unknown or unsupported model families"):
                self.team.adopt_pr("demo", str(number), "revise", ["human"], grants=["edit"])
            run = self.team.adopt_pr("demo", str(number), "review", ["human"], grants=["github"])
            self.assertFalse(run["independence"]["established"])
            self.assertEqual(run["adopted_pr"]["unresolved_trailers"], [value])
            self.assertEqual(run["adopted_pr"]["trailer_families"], [])
            self.assertEqual(run["contributors"], ["human"])
            run = self.ticks(run, 3)
            self.assertEqual(run["stage"], "stopped")
            self.assertIsNone(run.get("reviewed_sha"))
            self.assertEqual(run["review_withheld"]["sha"], head)
            body = self.github.comments[(number, f"{run['id']}-review-0-{head}")]
            self.assertIn("Review (independence not established)", body)
            report = self.team.pr_report(run["id"])
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["authorship"]["unresolved_trailers"], [value])
            self.store.save(run, stage="closed")
        # A known family alongside an unsupported value still never reviews its own work.
        self.open_pr(9, "branch-9", message="Add feature\n\nAgent-Family: openai\nAgent-Family: gemini")
        run = self.team.adopt_pr("demo", "9", "review", ["human"])
        self.assertEqual((run["author"], run["reviewer"]), ("codex", "claude"))
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["openai"])
        self.assertFalse(run["independence"]["established"])

    def test_single_family_authorship_assigns_independent_roles(self):
        self.open_pr(message="Add feature\n\nAgent-Family: openai")
        with self.assertRaisesRegex(TeamError, "cannot review independently"):
            self.team.adopt_pr("demo", "7", "review", ["human"], reviewer="codex")
        run = self.team.adopt_pr("demo", "7", "review", ["human"])
        self.assertEqual((run["author"], run["reviewer"]), ("codex", "claude"))
        self.assertEqual(run["contributors"], ["human", "openai"])

    def test_duplicate_adoption_and_conflicting_ownership_are_refused(self):
        self.open_pr()
        run = self.team.adopt_pr("demo", "7", "review", ["human"])
        with self.assertRaisesRegex(TeamError, f"already tracked by run {run['id']}"):
            self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"])
        self.store.save(run, stage="closed")
        with self.assertRaisesRegex(TeamError, "already has a run"):
            self.team.adopt_pr("demo", "7", "review", ["human"])
        owned = self.store.create(self.project, self.github.items[0])
        self.store.save(owned, pr=9, stage="stopped")
        with self.assertRaisesRegex(TeamError, f"already tracked by run {owned['id']}"):
            self.team.adopt_pr("demo", "9", "review", ["human"])
        self.assertEqual(len(self.store.runs()), 2)

    def test_readoption_after_stopped_exhausted_run_keeps_budget(self):
        self.store.update_project("demo", max_revisions=1)
        self.open_pr()
        # The existing head and its revision are both rejected (`True` counts as one rejection).
        self.agents.reject = 2
        run = self.until_handoff(self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"]))
        self.assertEqual((run["stage"], run["round"]), ("handoff", 1))
        run = self.team.decide(run["id"], "stop")
        self.assertEqual(run["stage"], "closed")
        # An external commit changes the adoption inputs; the exhausted budget still applies.
        external = self.push_external()
        self.store.update_project("demo", max_revisions=5)
        calls = list(self.agents.calls)
        with self.assertRaisesRegex(TeamError, f"reached its revision limit in run {run['id']}"):
            self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"])
        with self.assertRaisesRegex(TeamError, "reached its revision limit"):
            self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit"], findings=["Fix it"])
        self.assertEqual(len(self.store.runs()), 1)
        review = self.team.adopt_pr("demo", "7", "review", ["human"])
        self.assertEqual((review["sha"], review["round"], review["revision_limit"]), (external, 1, 1))
        self.assertEqual(review["prior_runs"][0]["id"], run["id"])
        self.assertEqual(review["prior_runs"][0]["handoffs"], 1)
        self.assertEqual(review["prior_runs"][0]["decisions"], ["stop"])
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
        plan = self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"], plan_only=True)
        self.assertEqual(plan["revision_budget"], {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        plan = self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit"], findings=["Fix it"],
                                  plan_only=True)
        self.assertEqual(plan["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        with self.assertRaisesRegex(TeamError, "no revision budget left"):
            self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit"], findings=["Fix it"])
        revised = self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"])
        self.assertEqual((revised["round"], revised["revision_limit"]), (2, 2))

    def test_closed_merged_and_incompatible_base_are_handled_before_mutation(self):
        self.open_pr()
        self.github.pulls[7]["state"] = "closed"
        with self.assertRaisesRegex(TeamError, "never reopens"):
            self.team.adopt_pr("demo", "7", "review", ["human"])
        self.github.pulls[7]["merged"] = True
        with self.assertRaisesRegex(TeamError, "merged"):
            self.team.adopt_pr("demo", "7", "review", ["human"])
        self.assertEqual(self.github.pulls[7]["state"], "closed")
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(8, "release-fix", base="release")
        with self.assertRaisesRegex(TeamError, "never retargets"):
            self.team.adopt_pr("demo", "8", "revise", ["human"], grants=["edit"])
        run = self.team.adopt_pr("demo", "8", "review", ["human"])
        self.assertEqual(run["adopted_pr"]["base_ref"], "release")
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        self.assertEqual(self.github.pulls[8]["base"], "release")

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
            with self.assertRaisesRegex(TeamError, message):
                self.team.select("demo", ["revision", "validate"], ["edit"], run_id=run["id"])
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
            self.assertNotIn("implement", [role for _, role in self.agents.calls])
            self.assertEqual(git(self.store.workspace(run), "rev-parse", "HEAD"), head)
            self.assertEqual(git(self.store.workspace(run), "status", "--porcelain"), "")
            self.assertEqual(self.remote_head(branch), head)

    def readiness_run(self):
        head = self.open_pr()
        run = self.reviewed(grants=["github"])
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        self.assertEqual(self.team.pr_report(run["id"])["ci_checks"], "not checked")
        return head, self.team.select("demo", ["ci"], ["github", "readiness"], run_id=run["id"])

    def test_readiness_records_successful_ci(self):
        head, run = self.readiness_run()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "ready")
        self.assertFalse(self.github.pulls[7]["draft"])
        report = self.team.pr_report(run["id"])
        check = report["current_ci"]
        self.assertEqual((check["operation"], check["head"], check["base"]), ("ci", head, run["base_sha"]))
        self.assertEqual((check["state"], check["current"], check["readiness_changed"]), ("success", True, True))
        self.assertEqual(report["ci_checks"], [check])
        self.assertFalse(any("CI checks" in item for item in report["limitations"]))

    def test_readiness_records_pending_and_failed_ci(self):
        head, run = self.readiness_run()
        self.github.check_state = "pending"
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "ci")
        self.assertTrue(self.github.pulls[7]["draft"])
        report = self.team.pr_report(run["id"])
        self.assertEqual(len(report["ci_checks"]), 1)
        self.assertEqual((report["current_ci"]["state"], report["current_ci"]["readiness_changed"]), ("pending", False))
        self.assertIn("GitHub CI checks for the current candidate did not pass (state: pending).", report["limitations"])
        self.github.check_state = "failure"
        try:
            run = self.ticks(run, 1)
        except TeamError:
            run = self.store.get(run["id"])
        self.assertNotEqual(run["stage"], "ready")
        self.assertTrue(self.github.pulls[7]["draft"])
        report = self.team.pr_report(run["id"])
        self.assertEqual([c["state"] for c in report["ci_checks"]], ["pending", "failure"])
        self.assertEqual(report["current_ci"]["head"], head)
        self.assertIn("GitHub CI checks for the current candidate did not pass (state: failure).", report["limitations"])

    def test_ci_observations_are_invalidated_by_movement(self):
        head, run = self.readiness_run()
        self.github.check_state = "pending"
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "ci")
        self.advance_base()
        try:
            run = self.ticks(run, 1)
        except TeamError:
            run = self.store.get(run["id"])
        self.assertEqual(run["stage"], "stale")
        report = self.team.pr_report(run["id"])
        self.assertIsNone(report["current_ci"])
        self.assertEqual((report["ci_checks"][0]["head"], report["ci_checks"][0]["current"]), (head, False))
        self.assertIn("GitHub CI checks were not checked for the current candidate and base; earlier "
                      "observations are historical.", report["limitations"])

    def test_movement_during_readiness_writes_stops_before_ready(self):
        for write, move in self.scenarios(("comment", "push_external"), ("status", "advance_base")):
            head, run = self.readiness_run()
            real = getattr(self.github, write)

            def moving(repo, target, key, *args, **kwargs):
                real(repo, target, key, *args, **kwargs)
                if "-review-" in key or key == "success":
                    getattr(self, move)()

            with patch.object(self.github, write, side_effect=moving):
                run = self.ticks(run, 1)
            self.assertEqual(run["stage"], "stale")
            self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))
            self.assertTrue(self.github.pulls[7]["draft"])
            self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
            self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
            self.assertFalse(run["ci_checks"][-1]["readiness_changed"])
            if write == "comment":
                self.assertNotIn((head, "success"), self.github.statuses)

    def test_movement_after_marking_ready_is_never_saved_as_ready(self):
        head, run = self.readiness_run()
        real = self.github.comment

        def moving(repo, number, marker, *args, **kwargs):
            real(repo, number, marker, *args, **kwargs)
            if marker.endswith("-ready"):
                self.push_external()

        with patch.object(self.github, "comment", side_effect=moving):
            run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertEqual(run["reviewed_sha"], None)
        # The draft change already happened; the report says so instead of claiming nothing changed.
        self.assertFalse(self.github.pulls[7]["draft"])
        self.assertTrue(run["ci_checks"][-1]["readiness_changed"])
        report = self.team.pr_report(run["id"])
        self.assertIn(f"Agent Team marked the PR ready for {head}; that readiness is not current.",
                      report["limitations"])
        self.assertFalse(any("did not change draft" in item for item in report["limitations"]))

    def test_concurrent_head_change_requires_deliberate_update(self):
        self.open_pr()
        run = self.reviewed(count=2)
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("pr update", run["error"])
        self.assertEqual(self.agents.calls, [])
        with self.assertRaisesRegex(TeamError, "pr update"):
            self.team.refresh(run["id"])
        run = self.team.update_pr(run["id"], ["human"])
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
        self.assertIsNone(run["validated_sha"])
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir())
        self.assertTrue(run["evidence_invalidations"])
        with self.assertRaisesRegex(TeamError, "unchanged"):
            self.team.update_pr(run["id"], ["human"])
        run = self.team.select("demo", ["validate", "review"], [], run_id=run["id"])
        run = self.ticks(run, 2)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", external))
        self.assertEqual(self.remote_head(), external)

    def test_base_movement_after_stopped_review_retires_evidence_and_is_never_merged(self):
        head = self.open_pr()
        old = git(self.remote, "rev-parse", "main")
        run = self.reviewed()
        self.assertEqual(run["reviewed_sha"], head)
        self.assertTrue(self.team.pr_report(run["id"])["independent_review_success"])
        base = self.advance_base()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("never merged implicitly", run["error"])
        self.assertEqual((run["reviewed_sha"], run["review_record"]), (None, None))
        report = self.team.pr_report(run["id"])
        self.assertFalse(report["current_evidence"] or report["independent_review_success"]
                         or report["validation"]["passed_for_candidate"])
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["head"], historical["base"], historical["verdict"]), (head, old, "pass"))
        self.assertIn("PR base changed", historical["reason"])
        self.assertEqual(len(self.agents.calls), 1)
        run = self.team.update_pr(run["id"], ["human"])
        self.assertEqual((run["sha"], run["base_sha"]), (head, base))
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assertIsNone(run["reviewed_sha"])
        self.assertEqual(self.remote_head(), head)
        report = self.team.pr_report(run["id"])
        self.assertTrue(any("base was not merged" in item for item in report["limitations"]))

    def test_head_moved_before_publication_is_never_overwritten(self):
        self.open_pr()
        self.agents.reject = 1
        run = self.ticks(self.writable(), 5)
        self.assertEqual(run["stage"], "publish")
        local = run["sha"]
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(len(self.agents.calls), 2)
        run = self.team.update_pr(run["id"], ["human"])
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["unpushed_local_commit"], local)
        self.assertEqual(git(Path(update["preserved"]), "rev-parse", "HEAD"), local)
        self.assertEqual(run["sha"], external)

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
        self.assertEqual(run["stage"], "blocked")
        self.assertEqual(run["pending_push_sha"], run["sha"])
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", run["sha"]))
        self.assertEqual(self.remote_head(), run["sha"])
        self.assertIsNone(run["pending_push_sha"])

    def test_interrupted_review_comment_is_retried_without_review_call(self):
        head = self.open_pr()
        run = self.reviewed(count=2, grants=["github"])
        real = self.github.comment

        def fail(repo, number, marker, *args, **kwargs):
            if "-review-" in marker:
                raise TeamError("GitHub unavailable")
            return real(repo, number, marker, *args, **kwargs)

        with patch.object(self.github, "comment", side_effect=fail), self.assertRaises(TeamError):
            self.ticks(run, 1)
        run = self.store.get(run["id"])
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(len(run["outbox"]), 1)
        run = self.ticks(run, 1)
        self.assertEqual(run["outbox"], [])
        self.assertIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertEqual(len(self.agents.calls), 1)

    def test_head_movement_during_review_keeps_evidence_local(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.reviewed(count=2, grants=["github"])
        self.assertEqual(run["stage"], "review")
        run = self.tick_moving_during_review(run)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("PR head moved", run["error"])
        self.assertIn("before review evidence was published", run["error"])
        self.assertNotIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual(run["outbox"], [])
        self.assertEqual({i["evidence"] for i in run["unpublished_evidence"]}, {head})
        report = self.team.pr_report(run["id"])
        self.assertEqual(len(report["unpublished_evidence"]), 2)
        self.assertTrue(any("was not published" in item for item in report["limitations"]))
        run = self.ticks(run, 2)
        self.assertEqual(run["stage"], "stale")
        self.assertEqual(self.github.comments, {})
        self.assertEqual(len(self.agents.calls), 1)
        run = self.team.update_pr(run["id"], ["human"])
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "validate"))

    def test_local_review_movement_retires_evidence(self):
        head = self.open_pr()
        base = git(self.remote, "rev-parse", "main")
        run = self.reviewed(count=2)
        self.assertEqual(run["stage"], "review")
        # Without a github grant nothing is queued for publication, yet the PR is still rechecked.
        run = self.tick_moving_during_review(run)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("PR head moved", run["error"])
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))
        self.assertEqual((self.github.comments, self.github.statuses), ({}, []))
        report = self.team.pr_report(run["id"])
        self.assertFalse(report["current_evidence"] or report["validation"]["passed_for_candidate"]
                         or report["independent_review_success"])
        self.assertEqual(report["validation"]["results"], [])
        self.assertTrue(any("historical" in item for item in report["limitations"]))
        historical = report["historical_evidence"][-1]
        self.assertEqual((historical["head"], historical["base"], historical["verdict"]), (head, base, "pass"))
        self.assertEqual((historical["validated"], historical["reviewed"]), (head, head))
        self.assertTrue(historical["independent_review_success"])
        # The full review stays readable, named with the commits it was gathered for.
        for review in (historical["review"], report["review"]):
            self.assertEqual((review["commit"], review["base"], review["current"]), (head, base, False))
            self.assertEqual(review["verdict"], "pass")
            self.assertTrue(review["summary"])
            self.assertIn("findings", review)
            self.assertEqual(review["reviewer"]["agent"], run["reviewer"])

    def test_deliberate_update_snapshots_review(self):
        head = self.open_pr()
        base = git(self.remote, "rev-parse", "main")
        run = self.reviewed()
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        external = self.push_external()
        # Update directly from the stopped run, before any tick notices the movement.
        run = self.team.update_pr(run["id"], ["human"])
        self.assertEqual(run["sha"], external)
        self.assertIsNone(run["review_record"])
        report = self.team.pr_report(run["id"])
        historical = report["historical_evidence"][-1]
        self.assertIn("Deliberate adoption", historical["reason"])
        self.assertEqual((historical["head"], historical["base"], historical["reviewed"]), (head, base, head))
        self.assertEqual(historical["review"]["verdict"], "pass")
        self.assertEqual(historical["review"]["reviewer"]["agent"], run["reviewer"])
        self.assertEqual((report["review"]["commit"], report["review"]["current"]), (head, False))
        self.assertFalse(report["independent_review_success"])

    def test_same_sha_head_identity_change_retires_evidence(self):
        for change in self.scenarios({"branch": "other-branch"}, {"head_repo": "someone/demo-fork"}):
            head = self.open_pr()
            run = self.reviewed(grants=["github"])
            self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
            if "branch" in change:
                git(self.remote, "branch", change["branch"], head)
            self.github.pulls[7].update(change)
            run = self.ticks(run, 1)
            self.assertEqual(run["stage"], "stale")
            self.assertIn("head repository or branch changed", run["error"])
            self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))
            report = self.team.pr_report(run["id"])
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
            self.assertIn((head, "pending", "PR head changed; review invalidated"),
                          self.github.status_descriptions)
            with self.assertRaisesRegex(TeamError, "adopt the PR again"):
                self.team.update_pr(run["id"], ["human"])

    def test_continuation_after_update_stops_on_rejection_without_revision(self):
        head = self.open_pr()
        run = self.ticks(self.writable(), 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        run = self.team.update_pr(run["id"], ["human"])
        run = self.team.select("demo", ["validate", "review"], [], run_id=run["id"])
        self.assertIsNone(run["pr_followup"])
        self.assertEqual(run["released_pr_followup"], ["revision", "validate", "publish", "review"])
        self.agents.reject = True
        run = self.ticks(run, 4)
        # The selected review endpoint holds: no edit, push, or repair loop.
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "implement"))
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual([role for _, role in self.agents.calls], ["review", "review"])
        self.assertEqual(run["sha"], external)
        self.assertEqual(self.remote_head(), external)
        self.assertEqual(run["adopted_pr"].get("pushed", []), [])

    def review_failing_head(self, mode, grants):
        """Adopt a PR whose head fails validation; both the failure and the review of that head are reported."""
        head = self.open_pr()
        self.store.update_project("demo", tests=["grep -q fixed feature.txt"])
        self.github.permissions["example/demo"] = True
        run = self.ticks(self.team.adopt_pr("demo", "7", mode, ["human"], grants=grants), 2)
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
        head, run = self.review_failing_head("revise", ["edit", "push", "github"])
        self.assertEqual(run["stage"], "revision")
        run = self.ticks(run, 4)
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review"), (run["author"], "implement"),
                                             (run["reviewer"], "review")])
        self.assertEqual((run["stage"], run["validated_sha"], run["reviewed_sha"]), ("stopped", run["sha"], run["sha"]))
        self.assertEqual(git(self.remote, "rev-parse", f"{run['sha']}^"), head)
        self.assertEqual(self.remote_head(), run["sha"])

    def test_review_only_reviews_failing_head_and_stops(self):
        head, run = self.review_failing_head("review", ["github"])
        # The stop boundary holds.
        self.assertEqual((run["stage"], run["next_stage"]), ("stopped", "implement"))
        run = self.ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual([role for _, role in self.agents.calls], ["review"])
        self.assertEqual(self.remote_head(), head)
        self.assertEqual(self.github.creates, 0)
        self.assertTrue(self.github.pulls[7]["draft"])
        report = self.team.pr_report(run["id"])
        self.assertTrue(report["review"]["validation_failed"])
        # The reviewer's own pass is reported as given; the candidate is rejected for failed validation.
        self.assertEqual((report["review"]["verdict"], report["review"]["summary"]), ("pass", self.agents.summary))
        self.assertEqual(report["review"]["findings"], [])
        self.assertEqual(report["review"]["candidate_verdict"], "changes_requested")
        self.assertEqual(report["review"]["candidate_findings"][0]["severity"], "validation")
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
            self.assertEqual(run["adopted_pr"]["unresolved_trailers"], unresolved)
            self.assertEqual(run["adoptions"][-1]["unresolved_trailers"], unresolved)
            # Trailer values are never recorded as contributors; declarations are.
            self.assertEqual(run["contributors"], sorted({"human", *declared}))
            # Revision is refused before any author call; review still reports findings only.
            with self.assertRaisesRegex(TeamError, "Independent review cannot be established"):
                self.team.select("demo", ["revision", "validate"], ["edit"], run_id=run["id"])
            self.agents.reject = False
            run = self.ticks(self.team.select("demo", ["validate", "review"], [], contributors=declared,
                                              run_id=run["id"]), 2)
            self.assertEqual(run["stage"], "stopped")
            self.assertIsNone(run.get("reviewed_sha"))
            self.assertEqual(run["review_withheld"]["sha"], external)
            self.assertNotIn("implement", [role for _, role in self.agents.calls])
            report = self.team.pr_report(run["id"])
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["authorship"]["unresolved_trailers"], unresolved)

    def test_unresolved_trailers_in_local_continuation_withhold_independence(self):
        head = self.open_pr()
        run = self.ticks(self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit"]), 3)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", head))
        cwd = self.store.workspace(run)
        (cwd / "local.txt").write_text("operator change\n")
        git(cwd, "add", ".")
        git(cwd, *COMMIT, "commit", "-m", "Local change\n\nAgent-Family: gemini")
        local = git(cwd, "rev-parse", "HEAD")
        run = self.team.select("demo", ["validate", "review"], [], contributors=["human"], run_id=run["id"])
        self.assertFalse(run["independence"]["established"])
        self.assertEqual(run["unresolved_trailers"], ["gemini"])
        self.assertEqual(run["adopted_pr"]["unresolved_trailers"], ["gemini"])
        self.assertIsNone(run["reviewed_sha"])
        run = self.ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertIsNone(run.get("reviewed_sha"))
        self.assertEqual(run["review_withheld"]["sha"], local)
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(self.team.pr_report(run["id"])["independent_review_success"])

    def test_base_movement_before_comment_retry_keeps_evidence_local(self):
        head = self.open_pr()
        run = self.reviewed(count=2, grants=["github"])
        with patch.object(self.github, "comment", side_effect=TeamError("GitHub unavailable")), \
                self.assertRaises(TeamError):
            self.ticks(run, 1)
        run = self.store.get(run["id"])
        self.assertEqual(len(run["outbox"]), 1)
        self.advance_base()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("PR base changed", run["error"])
        self.assertEqual(run["outbox"], [])
        self.assertNotIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertEqual(run["unpublished_evidence"][0]["evidence"], head)
        self.assertEqual(len(self.agents.calls), 1)

    def test_movement_during_first_outbox_write_withholds_later_evidence(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.reviewed(count=2, grants=["github"])
        self.assertEqual(run["stage"], "review")
        real = self.github.comment

        def moving(repo, number, marker, *args, **kwargs):
            result = real(repo, number, marker, *args, **kwargs)
            if "-review-" in marker:
                self.push_external()
            return result

        with patch.object(self.github, "comment", side_effect=moving):
            run = self.ticks(run, 1)
        # The comment went out before the move; the status that followed it did not.
        self.assertIn((7, f"{run['id']}-review-0-{head}"), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("PR head moved", run["error"])
        self.assertEqual(run["outbox"], [])
        self.assertEqual([i["type"] for i in run["unpublished_evidence"]], ["status"])
        self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
        self.assertIn("PR head moved", run["evidence_invalidations"][-1]["reason"])
        self.assertEqual(len(self.agents.calls), 1)

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

    def interrupted_update(self, point):
        """Interrupt update_pr at `point`, then recover by running the update again."""
        head = self.open_pr()
        run = self.reviewed()
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.interrupt(point, lambda: self.team.update_pr(run["id"], ["human"]))
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"]), ("stale", head))
        self.assertEqual(run["pending_swap"]["command"], "pr update RUN_ID")
        for command in (lambda: self.team.select("demo", ["validate"], [], run_id=run["id"]),
                        lambda: self.team.resume(run["id"])):
            with self.assertRaisesRegex(TeamError, "interrupted"):
                command()
        run = self.team.update_pr(run["id"], ["human"])
        self.assertIsNone(run["pending_swap"])
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
        self.assertEqual(git(self.store.workspace(run), "rev-parse", "HEAD"), external)
        self.assertEqual(git(Path(run["adopted_pr"]["updates"][0]["preserved"]), "rev-parse", "HEAD"), head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))
        self.assertEqual(run["evidence_context"]["head"], external)
        self.assertEqual(run["evidence_invalidations"][-1]["reason"], "Deliberate adoption of changed PR head or base")
        self.assertEqual(self.remote_head(), external)
        run = self.ticks(self.team.select("demo", ["validate", "review"], [], run_id=run["id"]), 2)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", external))

    def test_interrupted_update_recovers(self):
        for point in self.scenarios("journal", "rename-1", "rename-2", "final-save"):
            self.interrupted_update(point)

    def test_supplied_findings_are_revised_with_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=["Append a closing line to feature.txt"])
        self.assertEqual(run["operations"], ["revision", "validate", "publish", "review"])
        run = self.ticks(run, 5)
        self.assertEqual(run["stage"], "stopped")
        self.assertIn("Append a closing line to feature.txt", self.agents.prompts["implement"])
        self.assertIn("existing PR #7", self.agents.prompts["implement"])
        self.assertEqual(run["rejected_shas"] if "rejected_shas" in run else [], [])
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (run["sha"], run["sha"]))
        self.assertEqual(self.remote_head(), run["sha"])
        self.assertTrue(self.github.pulls[7]["draft"])

    def test_findings_mode_counts_its_first_edit_against_the_budget(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        with self.assertRaisesRegex(TeamError, "no revision budget left"):
            self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit"], findings=["Fix it"])
        self.assertEqual((self.store.runs(), self.agents.calls), ([], []))
        self.store.update_project("demo", max_revisions=1)
        self.agents.reject = True
        run = self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit"], findings=["Fix it"])
        self.assertEqual((run["round"], run["revision_limit"]), (1, 1))
        run = self.until_handoff(run)
        # Like revise mode with the same limit, exactly one revision is made before the handoff.
        self.assertEqual((run["stage"], run["round"]), ("handoff", 1))
        self.assertEqual([call for call in self.agents.calls if call[1] == "implement"], [(run["author"], "implement")])
        # The shared pre-edit guard also refuses a continuation past the budget, before any author call.
        self.store.save(run, stage="stopped", next_stage="revision", round=2, needs_revision=True)
        with self.assertRaisesRegex(TeamError, "no revision budget left"):
            self.team.select("demo", ["revision", "validate"], ["edit"], run_id=run["id"])
        self.assertEqual(len([call for call in self.agents.calls if call[1] == "implement"]), 1)

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
            with self.assertRaisesRegex(TeamError, "head repository or branch changed"):
                self.team.adopt(run["id"], ["human"])
            after = self.store.get(run["id"])
            self.assertEqual(after["stage"], "repair")
            self.assertFalse(after.get("adoptions"))
            self.assertEqual(after["adopted_pr"], run["adopted_pr"])
            self.assertEqual(git(self.store.workspace(after), "rev-parse", "HEAD"), before)
            self.assertEqual(list(self.store.run_root(after).glob("refresh-*")), [])

    def test_evidence_is_invalidated_by_configuration_change(self):
        self.open_pr()
        run = self.reviewed()
        self.store.update_project("demo", tests=["true"])
        with self.assertRaisesRegex(TeamError, "evidence invalidated"):
            self.team.select("demo", ["review"], [], run_id=run["id"])
        run = self.store.get(run["id"])
        self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (None, None))

    def test_exhausted_review_hands_off_and_repair_is_adopted_without_base_merge(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.agents.reject = True
        run = self.reviewed()
        self.assertEqual(run["stage"], "handoff")
        self.assertIn("feature.txt:1", run["handoffs"][0]["text"])
        run = self.team.decide(run["id"], "repair")
        self.assertEqual(run["stage"], "repair")
        external = self.push_external()
        self.advance_base()
        run = self.team.adopt(run["id"], ["human"])
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
        self.assertEqual(run["adopted_pr"]["head_sha"], external)
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assertEqual(self.remote_head(), external)

    def test_every_author_entry_needs_a_reserved_unused_round(self):
        for limit in self.scenarios(0, 2):
            self.store.update_project("demo", max_revisions=limit)
            head = self.open_pr()
            run = self.ticks(self.writable(), 3)
            self.assertEqual((run["stage"], run["reviewed_sha"], run["round"]), ("stopped", head, 0))
            # A passing review reserves no round, so no continuation can start an author pass.
            for operations in (["implement", "validate", "review"], ["revision", "validate"]):
                with self.assertRaisesRegex(TeamError, "No revision round is reserved"):
                    self.team.select("demo", operations, ["edit"], run_id=run["id"])
            self.store.save(run, round=limit + 1, reserved_round=limit + 1, needs_revision=True)
            with self.assertRaisesRegex(TeamError, "no revision budget left"):
                self.team.select("demo", ["implement", "validate"], ["edit"], run_id=run["id"])
            self.assertEqual([role for _, role in self.agents.calls], ["review"])
            self.assertEqual(self.remote_head(), head)

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
        self.assertEqual([role for _, role in self.agents.calls], ["implement", "review"])
        self.assertIn("supplied findings remain the revision scope", run["partial_result"])
        report = self.team.pr_report(run["id"])
        self.assertEqual(report["requested_findings"], ["Append a closing line to feature.txt"])
        self.assertEqual(report["review"]["findings"][0]["location"], "other.txt:3")
        # A round whose author pass already ran can never start another one.
        self.store.save(run, round=1, reserved_round=1)
        with self.assertRaisesRegex(TeamError, "each round allows one pass"):
            self.team.select("demo", ["revision", "validate"], ["edit"], run_id=run["id"])
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
            with self.assertRaisesRegex(TeamError, message):
                self.team.adopt_pr("demo", "7", "revise", second, grants=["edit"], reviewer="codex")
            again = self.team.adopt_pr("demo", "7", "review", second)
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
        report = self.team.pr_report(run["id"])
        self.assertFalse(report["current_evidence"] or report["review"]["current"])
        self.assertEqual((report["review"]["commit"], report["review"]["verdict"]), (head, "changes_requested"))
        # The handoff decision and its budget stay available.
        self.assertEqual(self.team.decide(run["id"], "repair")["stage"], "repair")

    def test_closed_or_merged_during_review_retires_evidence(self):
        for merged in self.scenarios(False, True):
            head = self.open_pr()
            run = self.reviewed(count=2, grants=["github"])
            with patch.object(self, "push_external",
                              lambda: self.github.pulls[7].update(state="closed", merged=merged)):
                run = self.tick_moving_during_review(run)
            self.assertEqual((run["stage"], run["reviewed_sha"]), ("merged" if merged else "closed", None))
            self.assertEqual((run["outbox"], self.github.comments), ([], {}))
            report = self.team.pr_report(run["id"])
            self.assertFalse(report["current_evidence"] or report["independent_review_success"]
                             or report["review"]["current"])
            self.assertEqual((report["review"]["commit"], report["historical_evidence"][-1]["verdict"]), (head, "pass"))
            self.assertEqual(self.github.pulls[7]["state"], "closed")

    def test_queued_evidence_is_withheld_after_configuration_or_pin_change(self):
        for change, reason in self.scenarios(("configuration", "Validation configuration changed"),
                                             ("pins", "Companion pins changed")):
            head = self.open_pr()
            run = self.reviewed(count=2, grants=["github"])
            with patch.object(self.github, "comment", side_effect=TeamError("GitHub unavailable")), \
                    self.assertRaises(TeamError):
                self.ticks(run, 1)
            self.assertEqual(len(self.store.get(run["id"])["outbox"]), 1)
            if change == "configuration":
                self.store.update_project("demo", tests=["true"])
                run = self.ticks(run, 1)
            else:
                with patch.object(self.team, "pins_changed", return_value=True):
                    run = self.ticks(run, 1)
            self.assertEqual((run["stage"], run["next_stage"], run["outbox"]), ("stopped", "validate", []))
            self.assertEqual(self.github.comments, {})
            self.assertEqual(run["unpublished_evidence"][0]["withheld_reason"], reason)
            report = self.team.pr_report(run["id"])
            self.assertFalse(report["current_evidence"] or report["independent_review_success"])
            self.assertEqual((report["review"]["commit"], report["review"]["current"]), (head, False))
            self.assertEqual(report["historical_evidence"][-1]["reason"], reason)
            # The saved review is never rerun: new validation must be selected explicitly first.
            with self.assertRaisesRegex(TeamError, "validat"):
                self.team.select("demo", ["review"], [], run_id=run["id"])
            self.assertEqual(len(self.agents.calls), 1)

    def test_interrupted_repair_adoption_recovers_recorded_inputs(self):
        for point in self.scenarios("journal", "rename-1", "rename-2", "final-save"):
            run = self.repair_handoff()
            head = run["sha"]
            external = self.push_external(message="Repair")
            self.interrupt(point, lambda: self.team.adopt(run["id"], ["human"]))
            stored = self.store.get(run["id"])
            self.assertEqual((stored["stage"], stored["sha"]), ("repair", head))
            self.assertEqual(stored["pending_swap"]["command"], "adopt RUN_ID")
            with self.assertRaisesRegex(TeamError, "interrupted"):
                self.team.decide(run["id"], "stop")
            # The PR moves again before recovery; recovery still installs the journaled inputs.
            later = self.commit("feature", "feature", "later.txt", "later\n", "Later change")
            run = self.team.adopt(run["id"], ["human"])
            self.assertIsNone(run["pending_swap"])
            self.assertEqual((run["stage"], run["next_stage"], run["sha"], run["round"]),
                             ("stopped", "validate", external, 1))
            self.assertEqual([(a["head"], a["declared"]) for a in run["adoptions"]], [(external, ["human"])])
            self.assertEqual(run["adopted_pr"]["head_sha"], external)
            self.assertEqual(git(self.store.workspace(run), "rev-parse", "HEAD"), external)
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
        args = parser().parse_args(["pr", "update", "RUN", "--contributor", "human"])
        self.assertEqual(args.run_id, "RUN")
        with self.assertRaises(SystemExit):
            parser().parse_args(["pr", "review", "demo", "7", "--contributor", "human", "--grant", "readiness"])
        args = parser().parse_args(["adopt", "RUN", "--contributor", "unknown"])
        self.assertEqual(args.contributor, ["unknown"])
        # `unknown` is accepted only for adopted PRs; new issue or task selections still refuse it.
        with self.assertRaisesRegex(TeamError, "Unknown contributor"):
            self.team.select("demo", ["validate"], [], task="Validate", ref="main", contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
