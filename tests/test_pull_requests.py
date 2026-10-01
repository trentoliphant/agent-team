"""Adoption of existing pull requests. Local Git fixtures and fake providers only; no model or GitHub calls."""
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from agent_team.cli import parser
from agent_team.coordinator import Coordinator, pull_number
from agent_team.github import GitHub
from agent_team.process import execute, git, TeamError
from agent_team.state import Store

from tests.test_coordinator import FakeAgents, FakeGitHub

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


class PullRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        execute(["git", "init", "-b", "main", str(self.source)])
        (self.source / "README.md").write_text("Fixture\n")
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", "Initial")
        self.remote = self.root / "remote.git"
        execute(["git", "clone", "--bare", str(self.source), str(self.remote)])
        self.store = Store(self.root / "state")
        self.project = self.store.register("demo", "example/demo", "main", ["test -f feature.txt"])
        self.github = PullGitHub(self.remote)
        self.agents = FakeAgents()
        self.team = Coordinator(self.store, self.github, self.agents)

        def local_clone(repo, destination, base, timeout):
            return execute(["git", "clone", "--branch", base, str(self.remote), str(destination)], timeout=timeout)

        self.clone_patch = patch("agent_team.coordinator.clone_repository", side_effect=local_clone)
        self.git_patch = patch("agent_team.coordinator.git", side_effect=self.local_git)
        self.clone_patch.start()
        self.git_patch.start()

    def tearDown(self):
        self.clone_patch.stop()
        self.git_patch.stop()
        self.store.db.close()
        self.tmp.cleanup()

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

    def open_pr(self, number=7, branch="feature", head_repo="example/demo", base="main", message="Add feature",
                user="octocat", maintainer_can_modify=False):
        git(self.source, "checkout", "-B", branch, git(self.remote, "rev-parse", base))
        (self.source / "feature.txt").write_text("external feature\n")
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", message)
        git(self.source, "push", str(self.remote), f"HEAD:refs/heads/{branch}")
        self.github.pulls[number] = {"title": "Existing feature", "body": "Human description", "state": "open",
                                     "merged": False, "draft": True, "user": user, "branch": branch,
                                     "head_repo": head_repo, "base": base,
                                     "maintainer_can_modify": maintainer_can_modify}
        return git(self.source, "rev-parse", "HEAD")

    def push_external(self, branch="feature", text="human follow-up\n"):
        git(self.source, "fetch", str(self.remote), branch)
        git(self.source, "checkout", "-B", branch, "FETCH_HEAD")
        (self.source / "external.txt").write_text(text)
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", "External change")
        git(self.source, "push", str(self.remote), f"HEAD:refs/heads/{branch}")
        return git(self.source, "rev-parse", "HEAD")

    def advance_base(self):
        git(self.source, "checkout", "-B", "main", git(self.remote, "rev-parse", "main"))
        (self.source / "base.txt").write_text("Updated base\n")
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", "Update base")
        git(self.source, "push", str(self.remote), "HEAD:refs/heads/main")
        return git(self.source, "rev-parse", "HEAD")

    def ticks(self, run, count):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def remote_head(self, branch="feature"):
        return git(self.remote, "rev-parse", f"refs/heads/{branch}")

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
        run = self.team.adopt_pr("https://github.com/example/demo/pull/7".split("/")[-1] and "demo",
                                 "https://github.com/example/demo/pull/7", "review", ["human"], grants=["github"])
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
        self.assertEqual(report["review"]["findings"][0]["location"], "feature.txt:1")

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
        self.github.permissions["example/demo"] = True
        self.agents.reject = 1
        run = self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit", "push", "github"])
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
        for number, value in ((7, "unknown"), (8, "gemini")):
            with self.subTest(value=value):
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

    def test_concurrent_head_change_requires_deliberate_update(self):
        self.open_pr()
        run = self.team.adopt_pr("demo", "7", "review", ["human"])
        run = self.ticks(run, 2)
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

    def test_base_movement_is_detected_and_never_merged(self):
        head = self.open_pr()
        run = self.ticks(self.team.adopt_pr("demo", "7", "review", ["human"]), 3)
        self.assertEqual(run["reviewed_sha"], head)
        base = self.advance_base()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")
        self.assertIn("never merged implicitly", run["error"])
        run = self.team.update_pr(run["id"], ["human"])
        self.assertEqual((run["sha"], run["base_sha"]), (head, base))
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assertIsNone(run["reviewed_sha"])
        self.assertEqual(self.remote_head(), head)
        report = self.team.pr_report(run["id"])
        self.assertTrue(any("base was not merged" in item for item in report["limitations"]))

    def test_head_moved_before_publication_is_never_overwritten(self):
        self.open_pr()
        self.github.permissions["example/demo"] = True
        self.agents.reject = 1
        run = self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit", "push", "github"])
        run = self.ticks(run, 5)
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
        self.github.permissions["example/demo"] = True
        self.agents.reject = 1
        run = self.team.adopt_pr("demo", "7", "revise", ["human"], grants=["edit", "push", "github"])
        run = self.ticks(run, 5)
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
        run = self.team.adopt_pr("demo", "7", "review", ["human"], grants=["github"])
        run = self.ticks(run, 2)
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
        run = self.team.adopt_pr("demo", "7", "review", ["human"], grants=["github"])
        run = self.ticks(run, 2)
        self.assertEqual(run["stage"], "review")
        real = self.agents.run

        def moving(agent, role, *args, **kwargs):
            if role == "review":
                self.push_external()
            return real(agent, role, *args, **kwargs)

        with patch.object(self.agents, "run", side_effect=moving):
            run = self.ticks(run, 1)
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

    def test_base_movement_before_comment_retry_keeps_evidence_local(self):
        head = self.open_pr()
        run = self.team.adopt_pr("demo", "7", "review", ["human"], grants=["github"])
        run = self.ticks(run, 2)
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

    def interrupted_update(self, point):
        """Interrupt update_pr at `point`, then recover by running the update again."""
        head = self.open_pr()
        run = self.ticks(self.team.adopt_pr("demo", "7", "review", ["human"]), 3)
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual(run["stage"], "stale")

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
            if point == "final-save" and "pending_pr_update" in changes and changes["pending_pr_update"] is None:
                raise Interrupted()
            real_save(record, **changes)
            if point == "journal" and changes.get("pending_pr_update"):
                raise Interrupted()

        with patch("pathlib.Path.rename", rename), patch.object(self.store, "save", side_effect=save), \
                self.assertRaises(Interrupted):
            self.team.update_pr(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"]), ("stale", head))
        self.assertTrue(run["pending_pr_update"])
        with self.assertRaisesRegex(TeamError, "interrupted"):
            self.team.select("demo", ["validate"], [], run_id=run["id"])
        with self.assertRaisesRegex(TeamError, "interrupted"):
            self.team.resume(run["id"])
        run = self.team.update_pr(run["id"], ["human"])
        self.assertIsNone(run["pending_pr_update"])
        self.assertEqual((run["stage"], run["next_stage"], run["sha"]), ("stopped", "validate", external))
        self.assertEqual(git(self.store.workspace(run), "rev-parse", "HEAD"), external)
        self.assertEqual(git(Path(run["adopted_pr"]["updates"][0]["preserved"]), "rev-parse", "HEAD"), head)
        self.assertIsNone(run["validated_sha"])
        self.assertIsNone(run["reviewed_sha"])
        self.assertEqual(run["evidence_context"]["head"], external)
        self.assertEqual(run["evidence_invalidations"][-1]["reason"], "Deliberate adoption of changed PR head or base")
        self.assertEqual(self.remote_head(), external)
        run = self.ticks(self.team.select("demo", ["validate", "review"], [], run_id=run["id"]), 2)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("stopped", external))

    def test_update_interrupted_after_journal_recovers(self):
        self.interrupted_update("journal")

    def test_update_interrupted_after_first_rename_recovers(self):
        self.interrupted_update("rename-1")

    def test_update_interrupted_after_second_rename_recovers(self):
        self.interrupted_update("rename-2")

    def test_update_interrupted_before_final_save_recovers(self):
        self.interrupted_update("final-save")

    def test_supplied_findings_are_revised_with_fresh_evidence(self):
        head = self.open_pr()
        self.github.permissions["example/demo"] = True
        run = self.team.adopt_pr("demo", "7", "findings", ["human"], grants=["edit", "push", "github"],
                                 findings=["Append a closing line to feature.txt"])
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

    def test_evidence_is_invalidated_by_configuration_change(self):
        self.open_pr()
        run = self.ticks(self.team.adopt_pr("demo", "7", "review", ["human"]), 3)
        self.store.update_project("demo", tests=["true"])
        with self.assertRaisesRegex(TeamError, "evidence invalidated"):
            self.team.select("demo", ["review"], [], run_id=run["id"])
        run = self.store.get(run["id"])
        self.assertIsNone(run["validated_sha"])
        self.assertIsNone(run["reviewed_sha"])

    def test_exhausted_review_hands_off_and_repair_is_adopted_without_base_merge(self):
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.agents.reject = True
        run = self.ticks(self.team.adopt_pr("demo", "7", "review", ["human"]), 3)
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


if __name__ == "__main__":
    unittest.main()
