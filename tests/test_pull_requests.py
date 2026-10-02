"""Adoption of existing pull requests. Local Git fixtures and fake providers only; no model or GitHub calls."""
from contextlib import nullcontext
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from agent_team.cli import parser
from agent_team.coordinator import Coordinator, pull_number, review_comment
from agent_team.github import GitHub
from agent_team.patches import COMPACT_NOTICE
from agent_team.process import git, TeamError

# A module import keeps discovery from running WorkflowTests here again.
from tests import test_coordinator
from tests.test_coordinator import FakeGitHub

COMMIT = ["-c", "user.name=Human", "-c", "user.email=human@example.invalid", "-c", "commit.gpgsign=false"]
ALL = ["edit", "push", "github"]
FIX = ["Append a closing line to feature.txt"]
FORK = "someone/demo-fork"
HISTORICAL_CI = "GitHub CI checks were not checked for the current candidate and base; earlier observations are historical."
DELIBERATE = "Deliberate adoption of changed PR head or base"
DRIFT = "Candidate or configuration changed"


class PullGitHub(FakeGitHub):
    """PR heads are branches in the local bare remote, including simulated forks."""

    def __init__(self, remote):
        super().__init__(remote)
        self.pulls = {}
        self.permissions = {}

    def pr(self, repo, number):
        return GitHub.pr(self, repo, number) if number in self.pulls else super().pr(repo, number)

    def api(self, endpoint):
        """GitHub's PR payload; its base SHA stays frozen at opening."""
        if "/git/ref/heads/" in endpoint:
            return {"object": {"sha": self.sha(endpoint.split("/git/ref/", 1)[1])}}
        number = int(endpoint.rsplit("/", 1)[1])
        p = self.pulls[number]
        return {"number": number, "title": p["title"], "body": p["body"], "state": p["state"],
                "merged": p["merged"], "draft": p["draft"], "user": {"login": p["user"]},
                "html_url": f"https://github.com/example/demo/pull/{number}",
                "maintainer_can_modify": p["maintainer_can_modify"],
                "head": {"sha": self.sha(f"heads/{p['branch']}"), "ref": p["branch"],
                         "repo": {"full_name": p["head_repo"]}},
                "base": {"ref": p["base"], "sha": p["frozen_base"]}}

    def sha(self, ref):
        return git(self.remote, "rev-parse", f"refs/{ref}")

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
        """Run each case as a subtest on a fresh fixture."""
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
            # GitHub exposes every PR head, including forks, as refs/pull/N/head.
            args = [f"refs/heads/{self.github.pulls[int(m[1])]['branch']}"
                    if (m := re.fullmatch(r"refs/pull/(\d+)/head", str(a))) else a for a in args]
        return git(cwd, *args)

    def commit(self, branch, start, name, text, message):
        git(self.source, "fetch", str(self.remote), start)
        git(self.source, "checkout", "-B", branch, "FETCH_HEAD")
        (self.source / name).write_text(text)
        git(self.source, "add", ".")
        git(self.source, *COMMIT, "commit", "-m", message)
        git(self.source, "push", str(self.remote), f"HEAD:refs/heads/{branch}")
        return self.head_of(self.source)

    def open_pr(self, number=7, branch="feature", head_repo="example/demo", base="main", message="Add feature",
                user="octocat", maintainer_can_modify=False):
        self.github.pulls[number] = {"title": "Existing feature", "body": "Human description", "state": "open",
                                     "merged": False, "draft": True, "user": user, "branch": branch,
                                     "head_repo": head_repo, "base": base, "frozen_base": self.remote_head(base),
                                     "maintainer_can_modify": maintainer_can_modify}
        # The branch name keeps heads of different PRs distinct.
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
        return self.head_of(cwd)

    def change(self, kind, run=None):
        """Change one evidence input of PR #7 (callers patch `pins` and `worker`)."""
        if kind == "identity":
            # The same commit on another branch, which the PR now uses.
            git(self.remote, "branch", "other-branch", "feature")
            self.github.pulls[7]["branch"] = "other-branch"
        elif kind == "repository":
            self.github.pulls[7]["head_repo"] = FORK
        elif kind == "configuration":
            self.store.update_project("demo", tests=["true"])
        elif kind in {"head", "base", "local"}:
            return {"head": self.push_external, "base": self.advance_base,
                    "local": lambda: self.local_commit(run)}[kind]()

    def pinned(self, kind):
        return patch.object(self.team, "pins_changed", return_value=kind == "pins")

    def head_of(self, path):
        return git(path, "rev-parse", "HEAD")

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
        self.github.permissions["example/demo"] = True
        return self.adopt_pr(mode, grants=ALL, **options)

    def reviewed(self, number="7", count=3, contributors=("human",), **options):
        return self.ticks(self.adopt_pr("review", number, contributors, **options), count)

    def stopped_review(self, **options):
        head = self.open_pr()
        run = self.reviewed(**options)
        self.assert_fields(run, stage="stopped", reviewed_sha=head)
        return head, run

    def readiness_run(self):
        head, run = self.stopped_review(grants=["github"])
        self.assertEqual(self.report(run)["ci_checks"], "not checked")
        return head, self.select(run, ["ci"], ["github", "readiness"])

    def at_publish(self):
        """A validated revision of PR #7 waiting to be pushed."""
        self.open_pr()
        self.agents.reject = 1
        run = self.ticks(self.writable(), 5)
        self.assertEqual(run["stage"], "publish")
        return run

    def repair_handoff(self):
        """An exhausted review of PR #7 handed off for repair outside Agent Team."""
        self.store.update_project("demo", max_revisions=0)
        self.open_pr()
        self.agents.reject = True
        run = self.reviewed()
        self.assertEqual(run["stage"], "handoff")
        self.assertIn("feature.txt:1", run["handoffs"][0]["text"])
        run = self.team.decide(run["id"], "repair")
        self.assertEqual(run["stage"], "repair")
        return run

    def ticks(self, run, count):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def tick_raising(self, run, count=1):
        try:
            return self.ticks(run, count)
        except TeamError:
            return self.store.get(run["id"])

    def until_handoff(self, run):
        for _ in range(10):
            if run["stage"] == "handoff":
                break
            run = self.ticks(run, 1)
        return run

    def tick_moving_during_review(self, run, move=None):
        real = self.agents.run

        def moving(agent, role, *args, **kwargs):
            if role == "review":
                (move or self.push_external)()
            return real(agent, role, *args, **kwargs)

        with patch.object(self.agents, "run", side_effect=moving):
            return self.ticks(run, 1)

    def writes(self, method, when, action):
        """Run `action` right after each matching real GitHub write."""
        real = getattr(self.github, method)

        def wrapped(repo, target, key, *args, **kwargs):
            result = real(repo, target, key, *args, **kwargs)
            if when(key):
                action()
            return result

        return patch.object(self.github, method, side_effect=wrapped)

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

    def remote_head(self, branch="feature"):
        return git(self.remote, "rev-parse", f"refs/heads/{branch}")

    def roles(self):
        return [role for _, role in self.agents.calls]

    def revised_calls(self, run):
        return [(run["reviewer"], "review"), (run["author"], "implement"), (run["reviewer"], "review")]

    def marker(self, run, head, number=7, round_=0):
        return number, f"{run['id']}-review-{round_}-{head}"

    def assert_fields(self, record, **expected):
        self.assertEqual({k: record.get(k) for k in expected}, expected)

    def assert_limited(self, report, *texts):
        for text in texts:
            self.assertTrue(any(text in item for item in report["limitations"]), text)

    def assert_quiet(self):
        self.assertEqual((self.github.comments, self.github.statuses), ({}, []))

    def assert_no_success(self):
        self.assertNotIn("success", [state for _, state in self.github.statuses])

    def assert_untouched(self, head, number=7, branch="feature"):
        """No push, replacement PR, or draft change."""
        self.assertEqual((self.remote_head(branch), self.github.creates), (head, 0))
        self.assertTrue(self.github.pulls[number]["draft"])

    def assert_stop_boundary(self, run, head):
        """Later ticks start no repair loop, edit, push, or readiness change."""
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], self.roles()), ("stopped", ["review"]))
        self.assert_untouched(head)
        return run

    def assert_pushed_on(self, run, head):
        """The revision was pushed as a fast-forward of `head` on the existing branch."""
        self.assertEqual((self.remote_head(), git(self.remote, "rev-parse", f"{run['sha']}^")), (run["sha"], head))

    def assert_historical(self, report):
        self.assertFalse(report["current_evidence"] or report["independent_review_success"]
                         or report["validation"]["passed_for_candidate"])

    def assert_review(self, review, commit, current, verdict="pass", base=None, agent=None):
        self.assertEqual((review["commit"], review["current"], review["verdict"]), (commit, current, verdict))
        if base:
            self.assertEqual(review["base"], base)
        if agent:
            self.assertEqual(review["reviewer"]["agent"], agent)

    def assert_retired(self, report, head, base, reason, verdict="pass", **fields):
        """The latest historical evidence names its head and base; its review is not current."""
        self.assert_historical(report)
        historical = report["historical_evidence"][-1]
        self.assert_fields(historical, head=head, base=base, verdict=verdict, **fields)
        self.assertIn(reason, historical["reason"])
        return historical

    def assert_withheld(self, run, head):
        self.assertEqual((run["stage"], run.get("reviewed_sha"), run["review_withheld"]["sha"]), ("stopped", None, head))

    def test_review_only_reports_findings_and_stops(self):
        for rejected, grants in self.scenarios((False, []), (True, ["github"]), (True, [])):
            head = self.open_pr(user="claude-bot")
            self.agents.reject, self.agents.summary = rejected, "Feature text is wrong"
            run = self.adopt_pr(number="7" if not rejected else "https://github.com/example/demo/pull/7", grants=grants)
            # A GitHub username never implies a model family.
            self.assert_fields(run, pr=7, sha=head, base_sha=self.remote_head("main"), issue=None,
                               contributors=["human"])
            self.assert_fields(run["adopted_pr"], head_repo="example/demo", head_ref="feature", base_ref="main",
                               state="open", draft=True, mode="review")
            self.assertEqual(run["adopted_pr"]["github_identities"]["pr_author"], "claude-bot")
            self.assertTrue(run["independence"]["established"])
            run = self.ticks(run, 3)
            self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
            if grants:
                body = self.github.comments[self.marker(run, head)]
                for text in ("changes requested", "GitHub CI checks: not checked by this review.",
                             "not a readiness verdict"):
                    self.assertIn(text, body)
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
                self.assert_fields(run, stage="stopped", reviewed_sha=head)
                self.assert_fields(report, currency={"verified": True, "reason": None})
                self.assertTrue(report["independent_review_success"] and report["validation"]["passed_for_candidate"])
                continue
            self.assert_fields(run, stage="stopped", next_stage="implement", review_record=None)
            self.assertIn(head, run["rejected_shas"])
            self.assert_fields(review, summary="Feature text is wrong", candidate_verdict="changes_requested",
                               validation_failed=False)
            self.assertEqual(review["findings"][0]["location"], "feature.txt:1")

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
        self.assert_fields(run, operations=["validate", "review"],
                           pr_followup=["revision", "validate", "publish", "review"])
        run = self.ticks(run, 7)
        self.assert_fields(run, stage="stopped", reviewed_sha=run["sha"])
        self.assertEqual(self.agents.calls, self.revised_calls(run))
        self.assert_pushed_on(run, head)
        self.assert_fields(run["adopted_pr"], pushed=[run["sha"]])
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual((self.github.creates, self.github.pulls[7]["body"]), (0, "Human description"))
        self.assertTrue(self.github.pulls[7]["draft"])
        self.assertIn((run["sha"], "pending", "Revision pushed; independent review pending"),
                      self.github.status_descriptions)
        self.assert_no_success()
        self.assertIn(self.marker(run, run["sha"], round_=1), self.github.comments)
        # The rejected head's report stays in the history.
        report = self.report(run)
        self.assert_review(report["review"], run["sha"], True)
        self.assert_review(report["review_history"][0], head, False, "changes_requested", run["base_sha"], run["reviewer"])

    def test_fork_pushes_only_with_maintainer_edits_and_otherwise_hands_off_locally(self):
        for editable in self.scenarios(True, False):
            head = self.open_pr(8, "fork-feature", head_repo=FORK, maintainer_can_modify=editable)
            self.github.permissions["example/demo"] = editable
            plan = self.adopt_pr("revise", "8", grants=ALL, plan_only=True)
            self.assertEqual((plan["push"]["allowed"], self.store.runs()), (editable, []))
            self.assertIn("maintainer edits" if editable else FORK, plan["push"]["reason"])
            if editable:
                self.assertIn("publish", plan["revision_operations"])
                self.refuses("github", self.adopt_pr, "revise", "8", grants=["edit", "push"])
                continue
            self.agents.reject = 1
            run = self.adopt_pr("revise", "8", grants=ALL)
            self.assertEqual(run["adopted_pr"]["head_repo"], FORK)
            self.assertNotIn("publish", run["pr_followup"])
            run = self.ticks(run, 6)
            self.assertEqual(run["stage"], "stopped")
            self.assert_untouched(head, 8, "fork-feature")
            handoff = self.report(run)["local_handoff"]
            self.assert_fields(handoff, commit=run["sha"], builds_on=head, replacement_pr="not created")
            self.assertIn(FORK, handoff["reason"])
            self.assertIn("fixed", Path(handoff["patch"]).read_text())
            # Evidence for the unpublished commit never makes the PR ready.
            self.assertEqual((run["validated_sha"], run["reviewed_sha"]), (run["sha"], run["sha"]))
            self.refuses("published PR head", self.select, run, ["ci"], ["github", "readiness"])
            self.store.save(run, stage="ci", grants=["github", "readiness"], operations=["ci"], stop_after="ci")
            run = self.ticks(run, 1)
            self.assertEqual(run["stage"], "blocked")
            self.assertIn("published PR head", run["error"])
            self.assert_no_success()
            self.assertTrue(self.github.pulls[8]["draft"])

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
            # Trailers are not contributors; usernames imply no family.
            self.assertEqual(run["contributors"], contributors)
            self.assert_fields(run["adopted_pr"], unresolved_trailers=unresolved,
                               trailer_families=["anthropic"] if trailer == "anthropic" else [])
            self.assertFalse(run["independence"]["established"])
            self.assertIn(refusal, run["independence"]["reason"])
            run = self.ticks(run, 3)
            self.assert_withheld(run, head)
            body = self.github.comments[self.marker(run, head)]
            self.assertIn("Review (independence not established)", body)
            self.assertIn(f"Independent-review success withheld: {run['independence']['reason']}", body)
            self.assert_no_success()
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["authorship"]["unresolved_trailers"], unresolved)
            self.refuses("exact-commit independent review", self.select, run, ["ci"], ["readiness"])

    def test_single_family_authorship_assigns_independent_roles(self):
        self.open_pr(message="Add feature\n\nAgent-Family: openai")
        self.refuses("cannot review independently", self.adopt_pr, reviewer="codex")
        run = self.adopt_pr()
        self.assert_fields(run, author="codex", reviewer="claude", contributors=["human", "openai"])
        self.store.save(run, stage="closed")
        # A known family still never reviews its own work.
        self.open_pr(9, "branch-9", message="Add feature\n\nAgent-Family: openai\nAgent-Family: gemini")
        run = self.adopt_pr(number="9")
        self.assert_fields(run, author="codex", reviewer="claude")
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["openai"])
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
        self.assert_fields(run, stage="handoff", round=1)
        self.assertEqual(self.team.decide(run["id"], "stop")["stage"], "closed")
        # The exhausted budget still applies after an external commit.
        external = self.push_external()
        self.store.update_project("demo", max_revisions=5)
        calls = list(self.agents.calls)
        self.refuses(f"reached its revision limit in run {run['id']}", self.adopt_pr, "revise", grants=["edit"])
        self.refuses("reached its revision limit", self.adopt_pr, "findings", grants=["edit"], findings=["Fix it"])
        self.assertEqual(len(self.store.runs()), 1)
        review = self.adopt_pr()
        self.assert_fields(review, sha=external, round=1, revision_limit=1)
        self.assert_fields(review["prior_runs"][0], id=run["id"], handoffs=1, decisions=["stop"])
        self.assertEqual(set(review["rejected_shas"]), set(run["rejected_shas"]))
        self.assertEqual(self.agents.calls, calls)

    def test_readoption_after_closed_run_continues_its_budget(self):
        self.store.update_project("demo", max_revisions=2)
        self.open_pr()
        self.agents.reject = True
        run = self.reviewed()
        self.assert_fields(run, stage="stopped", round=1)
        self.store.save(run, stage="closed")
        self.push_external()
        plan = self.adopt_pr("revise", grants=["edit"], plan_only=True)
        self.assertEqual(plan["revision_budget"], {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        plan = self.adopt_pr("findings", grants=["edit"], findings=["Fix it"], plan_only=True)
        self.assertEqual(plan["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        self.refuses("no revision budget left", self.adopt_pr, "findings", grants=["edit"], findings=["Fix it"])
        self.assert_fields(self.adopt_pr("revise", grants=["edit"]), round=2, revision_limit=2)

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
        self.assert_fields(self.ticks(run, 3), stage="stopped", reviewed_sha=head)
        self.assertEqual(self.github.pulls[8]["base"], "release")

    def test_closed_or_merged_pr_with_deleted_base_is_handled_explicitly(self):
        git(self.remote, "branch", "release", "main")
        self.open_pr(8, "release-fix", base="release")
        snapshot = self.github.pulls[8]["frozen_base"]
        git(self.remote, "branch", "-D", "release")
        # An open PR still needs its live base branch and fails closed without it.
        with self.assertRaises(TeamError):
            self.github.pr(None, 8)
        self.github.pulls[8]["state"] = "closed"
        self.assertEqual(self.github.pr(None, 8)["base"]["sha"], snapshot)
        self.refuses("never reopens", self.adopt_pr, number="8")
        self.github.pulls[8]["merged"] = True
        self.assertEqual(self.github.pr(None, 8)["base"]["sha"], snapshot)
        self.refuses("merged", self.adopt_pr, number="8")
        self.assertEqual(self.github.pulls[8]["state"], "closed")

    def test_revision_after_review_only_rejection_is_refused_before_editing(self):
        for number, branch, base, contributors, message in self.scenarios(
                (7, "feature", "main", ["human", "unknown"], "unknown contributors"),
                (8, "mixed", "main", ["openai", "anthropic"], "both model families"),
                (9, "release-fix", "release", ["human"], "never retargets")):
            git(self.remote, "branch", "release", "main")
            head = self.open_pr(number, branch, base=base)
            self.agents.reject = True
            run = self.reviewed(str(number), contributors=contributors)
            self.assert_fields(run, stage="stopped", next_stage="implement")
            calls = list(self.agents.calls)
            self.refuses(message, self.select, run, ["revision", "validate"], ["edit"])
            run = self.store.get(run["id"])
            self.assertEqual(run["stage"], "stopped")
            # Recovery into revision is refused by the same check.
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
            self.assertEqual((self.head_of(cwd), git(cwd, "status", "--porcelain"), self.remote_head(branch)),
                             (head, "", head))

    def test_readiness_records_successful_pending_and_failed_ci(self):
        for state in self.scenarios("success", "pending"):
            head, run = self.readiness_run()
            self.github.check_state = state
            run = self.ticks(run, 1 if state == "success" else 3)
            report = self.report(run)
            check = report["current_ci"]
            self.assert_fields(check, operation="ci", head=head, base=run["base_sha"], state=state)
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

    def test_ci_observations_stay_historical_after_movement_or_fresh_validation(self):
        for change in self.scenarios("base", "configuration", "pins"):
            if change == "base":
                head, run = self.readiness_run()
                self.github.check_state = "pending"
                self.assertEqual(self.ticks(run, 1)["stage"], "ci")
                self.advance_base()
                self.assertEqual(self.tick_raising(run)["stage"], "stale")
            else:
                head, run = self.stopped_review(grants=["github"])
                run = self.ticks(self.select(run, ["checks"]), 1)
                self.assert_fields(self.report(run)["current_ci"], operation="checks", state="success", head=head)
                if change == "pins":
                    self.team.invalidate_pins(self.store.project("demo"), run)
                else:
                    self.change(change)
                # New evidence does not renew the earlier CI observation.
                run = self.ticks(self.select(run, ["validate", "review"]), 2)
                self.assert_fields(run, stage="stopped", reviewed_sha=head)
                self.assertTrue(self.report(run)["current_evidence"])
            report = self.report(run)
            self.assertIsNone(report["current_ci"])
            self.assert_fields(report["ci_checks"][0], head=head, current=False)
            self.assertIn(HISTORICAL_CI, report["limitations"])

    def test_movement_during_or_after_readiness_writes_is_never_saved_as_ready(self):
        for write, trigger, move in self.scenarios(("comment", "-review-", "push_external"),
                                                   ("status", "success", "advance_base"),
                                                   ("comment", "-ready", "push_external")):
            head, run = self.readiness_run()
            with self.writes(write, lambda key: trigger in key, getattr(self, move)):
                run = self.ticks(run, 1)
            ready = trigger == "-ready"
            self.assert_fields(run, stage="stale", reviewed_sha=None)
            # A draft change that already happened is reported.
            self.assertEqual((self.github.pulls[7]["draft"], run["ci_checks"][-1]["readiness_changed"]),
                             (not ready, ready))
            if ready:
                limitations = self.report(run)["limitations"]
                self.assertIn(f"Agent Team marked the PR ready for {head}; that readiness is not current.",
                              limitations)
                self.assertFalse(any("did not change draft" in item for item in limitations))
                continue
            self.assertIsNone(run["validated_sha"])
            self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
            self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
            if write == "comment":
                self.assertNotIn((head, "success"), self.github.statuses)

    def test_pr_show_verifies_the_complete_binding_without_a_tick(self):
        for change in self.scenarios("head", "base", "identity", "configuration", "pins", "local", "worker", "ready"):
            if change == "ready":
                head, run = self.readiness_run()
                run = self.ticks(run, 1)
            else:
                head, run = self.stopped_review()
            report = self.report(run)
            self.assertTrue(report["current_evidence"] and report["independent_review_success"])
            self.change("head" if change == "ready" else change, run)
            busy = self.store.repository_lock("demo") if change == "worker" else nullcontext()
            with self.pinned(change), busy:
                report = self.report(run)
            self.assert_historical(report)
            self.assertIsNone(report["current_ci"])
            self.assert_review(report["review"], head, False)
            self.assertIs(report["currency"]["verified"], None if change == "worker" else False)
            self.assert_limited(report, "not verified" if change == "worker" else "historical")
            # Inspection never saves.
            self.assertEqual(self.store.get(run["id"]), run)

    def test_concurrent_head_change_requires_deliberate_update(self):
        self.open_pr()
        run = self.reviewed(count=2)
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual((run["stage"], self.agents.calls), ("stale", []))
        self.assertIn("pr update", run["error"])
        self.refuses("pr update", self.team.refresh, run["id"])
        run = self.update(run)
        self.assert_fields(run, stage="stopped", next_stage="validate", sha=external, validated_sha=None)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir() and run["evidence_invalidations"])
        self.refuses("unchanged", self.update, run)
        run = self.ticks(self.select(run, ["validate", "review"]), 2)
        self.assert_fields(run, stage="stopped", reviewed_sha=external)
        self.assertEqual(self.remote_head(), external)

    def test_base_movement_after_stopped_review_retires_evidence_and_is_never_merged(self):
        old = self.remote_head("main")
        head, run = self.stopped_review()
        base = self.advance_base()
        # The payload names the old base; the live branch counts.
        self.assertEqual(self.github.pr(None, 7)["base"]["snapshot_sha"], old)
        run = self.ticks(run, 1)
        self.assert_fields(run, stage="stale", reviewed_sha=None, review_record=None)
        self.assertIn("never merged implicitly", run["error"])
        self.assert_retired(self.report(run), head, old, "PR base changed")
        self.assertEqual(len(self.agents.calls), 1)
        run = self.update(run)
        self.assert_fields(run, sha=head, base_sha=base, reviewed_sha=None)
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assert_limited(self.report(run), "base was not merged")
        run = self.ticks(self.select(run, ["validate", "review"]), 2)
        self.assert_fields(run, stage="stopped", reviewed_sha=head, base_sha=base)
        self.assertTrue(self.report(run)["current_evidence"])

    def test_head_moved_before_publication_is_never_overwritten(self):
        run = self.at_publish()
        local = run["sha"]
        external = self.push_external()
        run = self.ticks(run, 1)
        self.assertEqual((run["stage"], self.remote_head(), len(self.agents.calls)), ("stale", external, 2))
        run = self.update(run)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual((update["unpushed_local_commit"], run["sha"], self.head_of(Path(update["preserved"]))),
                         (local, external, local))

    def test_interrupted_push_reconciles_without_force(self):
        run = self.at_publish()

        def lost_response(cwd, *args):
            result = self.local_git(cwd, *args)
            if "push" in args:
                self.assertFalse(any(str(a).startswith("+") or a == "--force" for a in args))
                raise TeamError("Simulated lost push response")
            return result

        with patch("agent_team.coordinator.git", side_effect=lost_response):
            run = self.ticks(run, 1)
        self.assert_fields(run, stage="blocked", pending_push_sha=run["sha"])
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_fields(run, stage="stopped", reviewed_sha=run["sha"], pending_push_sha=None)
        self.assertEqual(self.remote_head(), run["sha"])

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
            self.change(change)
            with self.pinned(change):
                run = self.ticks(run, 1)
            self.assertEqual((run["outbox"], len(self.agents.calls)), ([], 1))
            if change is None:
                self.assertIn(self.marker(run, head), self.github.comments)
                continue
            self.assertNotIn(self.marker(run, head), self.github.comments)
            self.assertEqual(run["unpublished_evidence"][0]["evidence"], head)
            if change == "base":
                self.assertEqual(run["stage"], "stale")
                self.assertIn("PR base changed", run["error"])
                continue
            self.assertEqual((run["stage"], run["next_stage"], self.github.comments), ("stopped", "validate", {}))
            self.assertEqual(run["unpublished_evidence"][0]["withheld_reason"], reasons[change])
            report = self.report(run)
            self.assert_review(report["review"], head, False)
            self.assertEqual(self.assert_retired(report, head, run["base_sha"], reasons[change])["reason"],
                             reasons[change])
            # The saved review is never rerun without new validation.
            self.refuses("validat", self.select, run, ["review"])
            self.assertEqual(len(self.agents.calls), 1)

    def test_movement_during_review_retires_evidence(self):
        """The PR moves, closes, or merges while its head is reviewed, with and without GitHub writes."""
        for case in self.scenarios("head", "local", "handoff", "closed", "merged"):
            if case == "handoff":
                self.store.update_project("demo", max_revisions=0)
            head, base = self.open_pr(), self.remote_head("main")
            rejected = self.agents.reject = case in {"head", "handoff"}
            closing = case in {"closed", "merged"}
            run = self.reviewed(count=2, grants=[] if case == "local" else ["github"])
            run = self.tick_moving_during_review(
                run, (lambda: self.github.pulls[7].update(state="closed", merged=case == "merged")) if closing else None)
            report = self.report(run)
            self.assertFalse(report["current_evidence"])
            self.assert_review(report["review"], head, False, "changes_requested" if rejected else "pass")
            self.assertEqual(self.github.comments, {})
            if case == "head":
                for text in ("PR head moved", "before review evidence was published"):
                    self.assertIn(text, run["error"])
                self.assertNotIn((head, "failure"), self.github.statuses)
                self.assertEqual((run["stage"], run["outbox"]), ("stale", []))
                self.assertEqual({i["evidence"] for i in run["unpublished_evidence"]}, {head})
                self.assertEqual(len(report["unpublished_evidence"]), 2)
                self.assert_limited(report, "was not published")
                run = self.ticks(run, 2)
                self.assertEqual((run["stage"], self.github.comments, len(self.agents.calls)), ("stale", {}, 1))
                self.assert_fields(self.update(run), stage="stopped", next_stage="validate")
            elif case == "local":
                # Without a github grant the PR is still rechecked.
                self.assert_fields(run, stage="stale", validated_sha=None, reviewed_sha=None)
                self.assertIn("PR head moved", run["error"])
                self.assertEqual((self.github.statuses, report["validation"]["results"]), ([], []))
                self.assert_limited(report, "historical")
                historical = self.assert_retired(report, head, base, "PR head moved", validated=head,
                                                 reviewed=head, independent_review_success=True)
                for review in (historical["review"], report["review"]):
                    self.assert_review(review, head, False, base=base, agent=run["reviewer"])
                    self.assertTrue(review["summary"])
                    self.assertIn("findings", review)
            elif case == "handoff":
                self.assertEqual((run["stage"], len(run["handoffs"]), run["outbox"], self.github.statuses),
                                 ("handoff", 1, [], []))
                self.assertIn(f"{run['id']}-handoff-0", [i.get("marker") for i in run["unpublished_evidence"]])
                self.assertIn("PR head moved", run["evidence_retired"]["reason"])
                # The handoff decision and its budget stay available.
                self.assertEqual(self.team.decide(run["id"], "repair")["stage"], "repair")
            else:
                self.assert_fields(run, stage=case, reviewed_sha=None, outbox=[])
                self.assert_retired(report, head, run["base_sha"], "")
                self.assertEqual(self.github.pulls[7]["state"], "closed")

    def test_movement_during_first_outbox_write_withholds_later_evidence(self):
        head = self.open_pr()
        self.agents.reject = True
        run = self.reviewed(count=2, grants=["github"])
        with self.writes("comment", lambda key: "-review-" in key, self.push_external):
            run = self.ticks(run, 1)
        # The comment went out before the move; the status that followed it did not.
        self.assertIn(self.marker(run, head), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual((run["stage"], run["outbox"], len(self.agents.calls)), ("stale", [], 1))
        self.assertEqual([i["type"] for i in run["unpublished_evidence"]], ["status"])
        self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
        for reason in (run["error"], run["evidence_invalidations"][-1]["reason"]):
            self.assertIn("PR head moved", reason)

    def test_deliberate_update_snapshots_review(self):
        base = self.remote_head("main")
        head, run = self.stopped_review()
        external = self.push_external()
        # Update before any tick notices the movement.
        run = self.update(run)
        self.assert_fields(run, sha=external, review_record=None)
        report = self.report(run)
        historical = self.assert_retired(report, head, base, DELIBERATE, reviewed=head)
        self.assert_review(historical["review"], head, False, agent=run["reviewer"])
        self.assert_review(report["review"], head, False)

    def test_persisted_rejection_with_changed_inputs_is_retired_on_recovery(self):
        # A crash between saving a rejecting verdict and recording it; then an input changes.
        for change, entry in self.scenarios(*((c, "update") for c in ("head", "base", "identity", "configuration",
                                                                     "local")), ("local", "resume")):
            head = self.open_pr()
            self.agents.reject = True
            run = self.ticks(self.adopt_pr("revise", grants=["edit"]), 2)
            with patch.object(self.team, "record_review", side_effect=TeamError("Interrupted")):
                run = self.ticks(run, 1)
            self.assert_fields(run, stage="blocked", review_sha=head)
            self.change(change, run)
            try:
                self.update(run) if entry == "update" else self.ticks(self.team.resume(run["id"]), 1)
            except TeamError:
                pass
            run = self.store.get(run["id"])
            self.assert_fields(run, round=0, review_record=None, needs_revision=False, revision_history=None)
            self.assertEqual((run.get("rejected_shas", []), self.roles()), ([], ["review"]))
            self.assertEqual([(h["head"], h["verdict"]) for h in self.report(run)["historical_evidence"]
                              if h["review"]], [(head, "changes_requested")])
            if change in {"head", "base"}:
                self.assert_fields(run, stage="stopped", next_stage="validate")

    def test_review_history_keeps_a_rejection_followed_by_validation_failure(self):
        self.store.update_project("demo", max_revisions=1, tests=["! grep -q fixed feature.txt"])
        head = self.open_pr()
        self.agents.reject = 1
        run = self.until_handoff(self.adopt_pr("revise", grants=["edit"]))
        self.assertEqual([e["kind"] for e in run["revision_history"]], ["review", "validation"])
        report = self.report(run)
        for review in (report["review"], *report["review_history"]):
            self.assert_review(review, head, False, "changes_requested", run["base_sha"], run["reviewer"])
            self.assert_fields(review, candidate_verdict="changes_requested", validation_failed=False)

    def test_local_continuation_and_validation_drift_keep_complete_prior_evidence(self):
        head, run = self.stopped_review()
        local = self.local_commit(run)
        run = self.select(run, ["validate", "review"], contributors=["human"])
        self.assert_review(self.report(run)["review"], head, False, agent=run["reviewer"])
        run = self.ticks(run, 2)
        report = self.report(run)
        historical = report["historical_evidence"][-1]
        self.assert_fields(historical, reason=DRIFT, validated=head, reviewed=head)
        self.assert_review(historical["review"], head, False, base=run["base_sha"], agent=run["reviewer"])
        self.assertTrue(historical["review"]["summary"] and report["independent_review_success"])
        self.assert_review(report["review"], local, True)
        self.store.save(run, stage="closed")
        # An edit after validation voids it; it is kept as history.
        head = self.open_pr(8, "drift")
        run = self.reviewed("8", count=2)
        (self.store.workspace(run) / "stray.txt").write_text("operator edit\n")
        run = self.ticks(run, 1)
        self.assert_fields(run, stage="stopped", next_stage="validate", validated_sha=None)
        self.assert_retired(self.report(run), head, run["base_sha"], DRIFT, verdict=None, validated=head)
        self.assertEqual(self.roles(), ["review", "review"])

    def test_same_sha_head_identity_change_retires_evidence(self):
        for change in self.scenarios("identity", "repository"):
            head, run = self.stopped_review(grants=["github"])
            self.change(change)
            run = self.ticks(run, 1)
            self.assert_fields(run, stage="stale", validated_sha=None, reviewed_sha=None)
            self.assertIn("head repository or branch changed", run["error"])
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
            self.assertIn((head, "pending", "PR head changed; review invalidated"), self.github.status_descriptions)
            self.refuses("adopt the PR again", self.update, run)

    def test_repair_adoption_refuses_changed_head_identity(self):
        for change in self.scenarios("repository", "identity"):
            run = self.repair_handoff()
            before = self.head_of(self.store.workspace(run))
            self.change(change)
            self.refuses("head repository or branch changed", self.team.adopt, run["id"], ["human"])
            after = self.store.get(run["id"])
            self.assert_fields(after, stage="repair", adoptions=None, adopted_pr=run["adopted_pr"])
            self.assertEqual(self.head_of(self.store.workspace(after)), before)
            self.assertEqual(list(self.store.run_root(after).glob("refresh-*")), [])

    def test_continuation_after_update_stops_on_rejection_without_revision(self):
        head = self.open_pr()
        run = self.ticks(self.writable(), 3)
        self.assert_fields(run, stage="stopped", reviewed_sha=head)
        external = self.push_external()
        self.assertEqual(self.ticks(run, 1)["stage"], "stale")
        self.update(run)
        run = self.select(run, ["validate", "review"])
        self.assert_fields(run, pr_followup=None, released_pr_followup=["revision", "validate", "publish", "review"])
        self.agents.reject = True
        run = self.ticks(run, 4)
        # The selected review endpoint holds: no edit, push, or repair loop.
        self.assert_fields(run, stage="stopped", next_stage="implement", sha=external)
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual(self.roles(), ["review", "review"])
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (external, []))

    def test_failing_head_is_reviewed_before_any_edit(self):
        """Adopt a PR whose head fails validation; the failure and the review are both reported."""
        for mode, grants in self.scenarios(("revise", ALL), ("review", ["github"])):
            head = self.open_pr()
            self.store.update_project("demo", tests=["grep -q fixed feature.txt"])
            self.github.permissions["example/demo"] = True
            run = self.ticks(self.adopt_pr(mode, grants=grants), 2)
            # The existing head is reviewed before any edit.
            self.assert_fields(run, stage="review", validated_sha=None)
            self.assertEqual((run["tests"][0]["exit_code"], run.get("rejected_shas", []), self.agents.calls), (1, [], []))
            run = self.ticks(run, 1)
            entry = run["revision_history"][0]
            self.assert_fields(entry, sha=head, kind="review", validation_failed=True)
            self.assertEqual((self.agents.calls, entry["findings"][0]["severity"], self.remote_head()),
                             ([(run["reviewer"], "review")], "validation", head))
            self.assertIn((head, "failure", "Configured validation failed"), self.github.status_descriptions)
            self.assertIn("exit 1", self.github.comments[self.marker(run, head)])
            if mode == "revise":
                self.assertEqual(run["stage"], "revision")
                run = self.ticks(run, 4)
                self.assertEqual(self.agents.calls, self.revised_calls(run))
                self.assert_fields(run, stage="stopped", validated_sha=run["sha"], reviewed_sha=run["sha"])
                self.assert_pushed_on(run, head)
                continue
            self.assert_fields(run, stage="stopped", next_stage="implement")
            report = self.report(self.assert_stop_boundary(run, head))
            review = report["review"]
            # The reviewer's pass is kept; the candidate is rejected for failed validation.
            self.assert_review(review, head, True)
            self.assert_fields(review, summary=self.agents.summary, findings=[], validation_failed=True,
                               candidate_verdict="changes_requested")
            self.assertEqual(review["candidate_findings"][0]["severity"], "validation")
            self.assertFalse(report["validation"]["passed_for_candidate"] or report["independent_review_success"])

    def test_unresolved_trailers_in_external_repair_withhold_independence(self):
        # A declared `unknown` repair contributor also withholds independence.
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
            # Revision is refused before any author call.
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
        self.assert_fields(run, stage="stopped", reviewed_sha=head)
        local = self.local_commit(run, "Local change\n\nAgent-Family: gemini")
        run = self.select(run, ["validate", "review"], contributors=["human"])
        self.assertFalse(run["independence"]["established"])
        self.assert_fields(run, unresolved_trailers=["gemini"], reviewed_sha=None)
        self.assertEqual(run["adopted_pr"]["unresolved_trailers"], ["gemini"])
        run = self.ticks(run, 2)
        self.assert_withheld(run, local)
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(self.report(run)["independent_review_success"])

    def test_interrupted_update_or_repair_adoption_recovers_recorded_inputs(self):
        points = ("journal", "rename-1", "rename-2", "final-save")
        for command, point in self.scenarios(*((c, p) for c in ("pr update RUN_ID", "adopt RUN_ID") for p in points)):
            updating = command == "pr update RUN_ID"
            if updating:
                head, run = self.stopped_review()
                external = self.push_external()
                self.assertEqual(self.ticks(run, 1)["stage"], "stale")
                recover = lambda: self.update(run)
            else:
                run = self.repair_handoff()
                head, external = run["sha"], self.push_external(message="Repair")
                recover = lambda: self.team.adopt(run["id"], ["human"])
            self.interrupt(point, recover)
            stored = self.store.get(run["id"])
            self.assert_fields(stored, stage="stale" if updating else "repair", sha=head)
            self.assertEqual(stored["pending_swap"]["command"], command)
            if updating:
                self.refuses("interrupted", self.select, run, ["validate"])
                self.refuses("interrupted", self.team.resume, run["id"])
            else:
                self.refuses("interrupted", self.team.decide, run["id"], "stop")
                # Recovery installs the journaled inputs even after another move.
                later = self.commit("feature", "feature", "later.txt", "later\n", "Later change")
            run = recover()
            self.assert_fields(run, pending_swap=None, stage="stopped", next_stage="validate", sha=external,
                               validated_sha=None, reviewed_sha=None)
            self.assertEqual((self.head_of(self.store.workspace(run)), run["adopted_pr"]["head_sha"]),
                             (external, external))
            preserved = self.store.run_root(run).glob("author-preserved-*")
            self.assertEqual([self.head_of(p) for p in preserved], [head])
            self.assertEqual(self.remote_head(), external if updating else later)
            if updating:
                self.assertEqual((run["evidence_context"]["head"], run["evidence_invalidations"][-1]["reason"]),
                                 (external, DELIBERATE))
                run = self.ticks(self.select(run, ["validate", "review"]), 2)
                self.assert_fields(run, stage="stopped", reviewed_sha=external)
                continue
            self.assertEqual(([(a["head"], a["declared"]) for a in run["adoptions"]], run["round"]),
                             ([(external, ["human"])], 1))
            run = self.ticks(run, 1)
            self.assertEqual(run["stage"], "stale")
            self.assertIn("agent-team adopt", run["error"])

    def test_supplied_findings_are_revised_with_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=FIX)
        self.assertEqual(run["operations"], ["revision", "validate", "publish", "review"])
        run = self.ticks(run, 5)
        self.assertEqual((run["stage"], run.get("rejected_shas", [])), ("stopped", []))
        for text in (FIX[0], "existing PR #7"):
            self.assertIn(text, self.agents.prompts["implement"])
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
        self.assert_fields(run, round=1, revision_limit=1)
        run = self.until_handoff(run)
        # Exactly one revision before the handoff, as in revise mode.
        self.assertEqual((run["stage"], run["round"], self.roles().count("implement")), ("handoff", 1, 1))
        # The pre-edit guard refuses a continuation past the budget.
        self.store.save(run, stage="stopped", next_stage="revision", round=2, needs_revision=True)
        self.refuses("no revision budget left", self.select, run, ["revision", "validate"], ["edit"])
        self.assertEqual(self.roles().count("implement"), 1)

    def test_evidence_is_invalidated_by_configuration_change(self):
        head, run = self.stopped_review()
        self.change("configuration")
        self.refuses("evidence invalidated", self.select, run, ["review"])
        self.assert_fields(self.store.get(run["id"]), validated_sha=None, reviewed_sha=None)

    def test_exhausted_review_hands_off_and_repair_is_adopted_without_base_merge(self):
        run = self.repair_handoff()
        external = self.push_external()
        self.advance_base()
        run = self.team.adopt(run["id"], ["human"])
        self.assert_fields(run, stage="stopped", next_stage="validate", sha=external)
        self.assert_fields(run["adopted_pr"], head_sha=external, base_contained=False)
        self.assertEqual(self.remote_head(), external)

    def test_every_author_entry_needs_a_reserved_unused_round(self):
        for limit in self.scenarios(0, 2):
            self.store.update_project("demo", max_revisions=limit)
            head = self.open_pr()
            run = self.ticks(self.writable(), 3)
            self.assert_fields(run, stage="stopped", reviewed_sha=head, round=0)
            # A passing review reserves no round.
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
        run = self.writable("findings", findings=FIX)
        self.assertIsNone(run["pr_followup"])
        run = self.ticks(run, 6)
        # The fresh finding is reported, not revised.
        self.assert_fields(run, stage="stopped", next_stage="implement", round=2)
        self.assertEqual(self.roles(), ["implement", "review"])
        self.assertIn("supplied findings remain the revision scope", run["partial_result"])
        report = self.report(run)
        self.assertEqual((report["requested_findings"], report["review"]["findings"][0]["location"]),
                         (FIX, "other.txt:3"))
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
            # New declarations never clear earlier recorded authorship.
            self.refuses(message, self.adopt_pr, "revise", contributors=second, grants=["edit"], reviewer="codex")
            again = self.adopt_pr(contributors=second)
            self.assertEqual(again["adopted_pr"]["inherited_provenance"]["runs"], [run["id"]])
            self.assertTrue(set(first) <= set(again["contributors"]))
            if first == ["openai"] and second == ["human"]:
                self.assert_fields(again, author="codex", reviewer="claude")
            else:
                self.assertIn(message, again["independence"]["reason"])

    def test_large_pr_is_reviewed_only_with_a_proven_complete_patch(self):
        lines = [f"line {n}" for n in range(300)]
        for compact_fits in self.scenarios(True, False):
            self.commit("main", "main", "big.txt", "\n".join(lines) + "\n", "Add big file")
            self.open_pr()
            edited = [f"edited {n}" if n % 50 == 0 else line for n, line in enumerate(lines)]
            head = self.commit("feature", "feature", "big.txt", "\n".join(edited) + "\n", "Edit big file")
            span = f"{self.remote_head('main')}...{head}"
            # `git` strips the final newline the review patch keeps.
            default, compact = (len(git(self.remote, "diff", *options, span)) + 1 for options in ([], ["--unified=0"]))
            with patch("agent_team.patches.REVIEW_BUDGET", compact if compact_fits else compact - 1):
                run = self.tick_raising(self.adopt_pr(), 3)
            if not compact_fits:
                # Never truncated: the review is refused before any reviewer runs.
                self.assert_fields(run, stage="blocked", reviewed_sha=None, review_record=None)
                self.assertIn("exceeds review budget even without context", run["error"])
                self.assertEqual(self.agents.calls, [])
                continue
            self.assert_fields(run, stage="stopped", reviewed_sha=head)
            self.assert_fields(run["review_record"]["patch"], format="compact", complete=True, files=2,
                               changed_lines=13, characters=compact, default_characters=default, range=span)
            prompt = self.agents.prompts["review"]
            for text in (COMPACT_NOTICE, "-line 50\n+edited 50\n"):
                self.assertIn(text, prompt)
            self.assertNotIn("\n line 51\n", prompt)
            self.assertEqual(self.report(run)["review"]["patch"], run["review_record"]["patch"])
            self.assertIn("complete context-free patch", review_comment(head, run["review_record"]))

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
        # `unknown` is accepted only for adopted PRs.
        self.refuses("Unknown contributor", self.team.select, "demo", ["validate"], [], task="Validate", ref="main",
                     contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
