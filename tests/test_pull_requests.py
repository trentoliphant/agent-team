"""Adoption of existing pull requests; offline fixtures only."""
from contextlib import nullcontext
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from agent_team.cli import parser
from agent_team.coordinator import LOCAL_CHANGE, Coordinator, pull_number, review_comment
from agent_team.github import GitHub
from agent_team.patches import COMPACT_NOTICE
from agent_team.process import git, TeamError

# A module import, so discovery does not rerun WorkflowTests here.
from tests import test_coordinator
from tests.test_coordinator import FakeGitHub

COMMIT = ["-c", "user.name=Human", "-c", "user.email=human@example.invalid", "-c", "commit.gpgsign=false"]
ALL = ["edit", "push", "github"]
FIX = ["Append a closing line to feature.txt"]
FORK = "someone/demo-fork"
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


class Interrupted(Exception):
    pass


class PullGitHub(FakeGitHub):
    def __init__(self, remote):
        super().__init__(remote)
        self.pulls = {}
        self.permissions = {}

    def pr(self, repo, number):
        return GitHub.pr(self, repo, number) if number in self.pulls else super().pr(repo, number)

    def api(self, endpoint):
        # Base SHA frozen at opening, as on GitHub.
        if "/git/ref/heads/" in endpoint:
            return {"object": {"sha": self.sha(endpoint.split("/git/ref/", 1)[1])}}
        number = int(endpoint.rsplit("/", 1)[1])
        p = self.pulls[number]
        return {**{k: p[k] for k in ("title", "body", "state", "merged", "draft", "maintainer_can_modify")},
                "number": number, "user": {"login": p["user"]},
                "html_url": f"https://github.com/example/demo/pull/{number}",
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
    tearDown = test_coordinator.WorkflowTests.tearDown
    command = test_coordinator.WorkflowTests.command

    def setUp(self):
        test_coordinator.WorkflowTests.setUp(self)
        self.source = self.root / "source"
        self.github = PullGitHub(self.remote)
        self.team = Coordinator(self.store, self.github, self.agents)
        self.git_patch.stop()
        self.git_patch = patch("agent_team.coordinator.git", side_effect=self.local_git)
        self.git_patch.start()

    def scenarios(self, *cases):
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
                user="octocat", maintainer_can_modify=False, reject=None):
        if reject is not None:
            self.agents.reject = reject
        self.github.pulls[number] = dict(
            title="Existing feature", body="Human description", state="open", merged=False, draft=True, user=user,
            branch=branch, head_repo=head_repo, base=base, frozen_base=self.remote_head(base),
            maintainer_can_modify=maintainer_can_modify)
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
        if kind == "identity":
            git(self.remote, "branch", "other-branch", "feature")
            self.github.pulls[7]["branch"] = "other-branch"
        elif kind == "repository":
            self.github.pulls[7]["head_repo"] = FORK
        elif kind == "configuration":
            self.configure(tests=["true"])
        elif kind == "dirty":
            (self.store.workspace(run) / "local.txt").write_text("operator edit\n")
        elif kind in {"head", "base", "local"}:
            return {"head": self.push_external, "base": self.advance_base,
                    "local": lambda: self.local_commit(run)}[kind]()

    def configure(self, **settings):
        self.store.update_project("demo", **settings)

    def reload(self, run):
        return self.store.get(run["id"])

    def pinned(self, kind):
        return patch.object(self.team, "pins_changed", return_value=kind == "pins")

    def head_of(self, path):
        return git(path, "rev-parse", "HEAD")

    def adopt_pr(self, mode="review", number="7", contributors=("human",), **options):
        return self.team.adopt_pr("demo", number, mode, list(contributors), **options)

    def editing(self, mode="revise", number="7", **options):
        return self.adopt_pr(mode, number, grants=["edit"], **options)

    def editing_findings(self, **options):
        return self.editing("findings", findings=["Fix it"], **options)

    def select(self, run, operations, grants=(), **options):
        return self.team.select("demo", list(operations), list(grants), run_id=run["id"], **options)

    def revalidate(self, run, **options):
        return self.ticks(self.select(run, ["validate", "review"], **options), 2)

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

    def under_review(self, reject=False, **options):
        head = self.open_pr(reject=reject)
        return head, self.reviewed(count=2, **options)

    def stopped_review(self, **options):
        head = self.open_pr()
        run = self.reviewed(**options)
        self.assert_reviewed(run, head)
        return head, run

    def readiness_run(self):
        head, run = self.stopped_review(grants=["github"])
        self.assertEqual(self.report(run)["ci_checks"], "not checked")
        return head, self.select(run, ["ci"], ["github", "readiness"])

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

    def swap_fixture(self, updating):
        if updating:
            head, run = self.stopped_review()
            external = self.push_external()
            self.assertEqual(self.ticks(run)["stage"], "stale")
            return run, head, external, lambda: self.update(run)
        run = self.repair_handoff()
        return run, run["sha"], self.push_external(message="Repair"), lambda: self.team.adopt(run["id"], ["human"])

    def ticks(self, run, count=1):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def tick_raising(self, run, count=1):
        try:
            return self.ticks(run, count)
        except TeamError:
            return self.reload(run)

    def until_handoff(self, run):
        for _ in range(10):
            if run["stage"] == "handoff":
                break
            run = self.ticks(run)
        return run

    def moving(self, owner, method, move, when=lambda *args: True, after=False):
        real = getattr(owner, method)

        def wrapped(*args, **kwargs):
            matched = when(*args)
            if matched and not after:
                move()
            result = real(*args, **kwargs)
            if matched and after:
                move()
            return result

        return patch.object(owner, method, side_effect=wrapped)

    def tick_moving_during_review(self, run, move=None):
        with self.moving(self.agents, "run", move or self.push_external, lambda _, role, *a: role == "review"):
            return self.ticks(run)

    def writes(self, method, when, action):
        return self.moving(self.github, method, action, lambda _, target, key, *a: when(key), after=True)

    def interrupt(self, point, action):
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

    def marker(self, run, head, round_=0):
        return 7, f"{run['id']}-review-{round_}-{head}"

    def assert_fields(self, record, **expected):
        self.assertEqual({k: record.get(k) for k in expected}, expected)

    def assert_reviewed(self, run, sha, **fields):
        self.assert_fields(run, stage="stopped", reviewed_sha=sha, **fields)

    def assert_awaiting(self, run, next_stage="validate", **fields):
        self.assert_fields(run, stage="stopped", next_stage=next_stage, **fields)

    def assert_status(self, sha, state, description):
        self.assertIn((sha, state, description), self.github.status_descriptions)

    def assert_contains(self, container, *texts):
        for text in texts:
            self.assertIn(text, container)

    def assert_limited(self, report, *texts):
        for text in texts:
            self.assertTrue(any(text in item for item in report["limitations"]), text)

    def assert_quiet(self):
        self.assertEqual((self.github.comments, self.github.statuses), ({}, []))

    def assert_no_success(self):
        self.assertNotIn("success", [state for _, state in self.github.statuses])

    def assert_untouched(self, head, number=7, branch="feature"):
        self.assertEqual((self.remote_head(branch), self.github.creates), (head, 0))
        self.assertTrue(self.github.pulls[number]["draft"])

    def assert_stop_boundary(self, run, head):
        run = self.ticks(run, 3)
        self.assertEqual((run["stage"], self.roles()), ("stopped", ["review"]))
        self.assert_untouched(head)
        return run

    def assert_pushed_on(self, run, head):
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
            self.github.pulls[number][field] = value
            check()
            self.refuses(message, self.adopt_pr, number=str(number))
        self.assertEqual(self.github.pulls[number]["state"], "closed")

    def refuses_revision(self, message, run):
        self.refuses(message, self.select, run, REVISION, ["edit"])

    def test_review_only_reports_findings_and_stops(self):
        for rejected, grants in self.scenarios((False, []), (True, ["github"]), (True, [])):
            head = self.open_pr(user="claude-bot")
            self.agents.reject, self.agents.summary = rejected, "Feature text is wrong"
            run = self.adopt_pr(number="7" if not rejected else "https://github.com/example/demo/pull/7", grants=grants)
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
                self.assert_contains(body, "changes requested", "GitHub CI checks: not checked by this review.",
                                     "not a readiness verdict")
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
                continue
            self.assert_awaiting(run, "implement", review_record=None)
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
        head = self.open_pr(reject=1)
        run = self.writable()
        self.assert_fields(run, operations=["validate", "review"], pr_followup=FOLLOWUP)
        run = self.ticks(run, 7)
        self.assert_reviewed(run, run["sha"])
        self.assertEqual(self.agents.calls, self.revised_calls(run))
        self.assert_pushed_on(run, head)
        self.assert_fields(run["adopted_pr"], pushed=[run["sha"]])
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual((self.github.creates, self.github.pulls[7]["body"]), (0, "Human description"))
        self.assertTrue(self.github.pulls[7]["draft"])
        self.assert_status(run["sha"], "pending", PUSHED)
        self.assert_no_success()
        self.assertIn(self.marker(run, run["sha"], round_=1), self.github.comments)
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
            self.assert_fields(run, stage="stopped", validated_sha=run["sha"], reviewed_sha=run["sha"])
            self.assert_untouched(head, 8, "fork-feature")
            handoff = self.report(run)["local_handoff"]
            self.assert_fields(handoff, commit=run["sha"], builds_on=head, replacement_pr="not created")
            self.assertIn(FORK, handoff["reason"])
            self.assertIn("fixed", Path(handoff["patch"]).read_text())
            self.refuses("published PR head", self.select, run, ["ci"], ["github", "readiness"])
            self.store.save(run, stage="ci", grants=["github", "readiness"], operations=["ci"], stop_after="ci")
            run = self.ticks(run)
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
            self.refuses(refusal, self.editing, contributors=declared)
            run = self.adopt_pr(contributors=declared, grants=["github"])
            self.assertEqual(run["contributors"], contributors)
            self.assert_fields(run["adopted_pr"], unresolved_trailers=unresolved,
                               trailer_families=["anthropic"] if trailer == "anthropic" else [])
            self.assertFalse(run["independence"]["established"])
            self.assertIn(refusal, run["independence"]["reason"])
            run = self.ticks(run, 3)
            self.assert_withheld(run, head, unresolved)
            body = self.github.comments[self.marker(run, head)]
            self.assert_contains(body, "Review (independence not established)",
                                 f"Independent-review success withheld: {run['independence']['reason']}")
            self.assert_no_success()
            self.refuses("exact-commit independent review", self.select, run, ["ci"], ["readiness"])

    def test_single_family_authorship_assigns_independent_roles(self):
        self.open_pr(message="Add feature\n\nAgent-Family: openai")
        self.refuses("cannot review independently", self.adopt_pr, reviewer="codex")
        run = self.adopt_pr()
        self.assert_fields(run, author="codex", reviewer="claude", contributors=["human", "openai"])
        self.store.save(run, stage="closed")
        self.open_pr(9, "branch-9", message="Add feature\n\nAgent-Family: openai\nAgent-Family: gemini")
        run = self.adopt_pr(number="9")
        self.assert_fields(run, author="codex", reviewer="claude")
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["openai"])
        self.assertFalse(run["independence"]["established"])

    def test_duplicate_adoption_and_conflicting_ownership_are_refused(self):
        self.open_pr()
        run = self.adopt_pr()
        self.refuses(f"already tracked by run {run['id']}", self.editing)
        self.store.save(run, stage="closed")
        self.refuses("already has a run", self.adopt_pr)
        owned = self.store.create(self.project, self.github.items[0])
        self.store.save(owned, pr=9, stage="stopped")
        self.refuses(f"already tracked by run {owned['id']}", self.adopt_pr, number="9")
        self.assertEqual(len(self.store.runs()), 2)

    def test_readoption_after_stopped_exhausted_run_keeps_budget(self):
        self.configure(max_revisions=1)
        # Rejects the existing head and its revision.
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
        self.assertEqual(set(review["rejected_shas"]), set(run["rejected_shas"]))
        self.assertEqual(self.agents.calls, calls)

    def test_readoption_after_closed_run_continues_its_budget(self):
        self.configure(max_revisions=2)
        self.open_pr(reject=True)
        run = self.reviewed()
        self.assert_fields(run, stage="stopped", round=1)
        self.store.save(run, stage="closed")
        self.push_external()
        plan = self.editing(plan_only=True)
        self.assertEqual(plan["revision_budget"], {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        self.assertEqual(self.editing_findings(plan_only=True)["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        self.refuses(NO_BUDGET, self.editing_findings)
        self.assert_fields(self.editing(), round=2, revision_limit=2)

    def test_closed_merged_and_incompatible_base_are_handled_before_mutation(self):
        self.open_pr()
        self.refuses_closed()
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(8, "release-fix", base="release")
        self.refuses("never retargets", self.editing, number="8")
        run = self.adopt_pr(number="8")
        self.assertEqual(run["adopted_pr"]["base_ref"], "release")
        self.assert_reviewed(self.ticks(run, 3), head)
        self.assertEqual(self.github.pulls[8]["base"], "release")

    def test_closed_or_merged_pr_with_deleted_base_is_handled_explicitly(self):
        git(self.remote, "branch", "release", "main")
        self.open_pr(8, "release-fix", base="release")
        snapshot = self.github.pulls[8]["frozen_base"]
        git(self.remote, "branch", "-D", "release")
        with self.assertRaises(TeamError):
            self.github.pr(None, 8)
        self.refuses_closed(8, lambda: self.assertEqual(self.github.pr(None, 8)["base"]["sha"], snapshot))

    def test_revision_after_review_only_rejection_is_refused_before_editing(self):
        for number, branch, base, contributors, message in self.scenarios(
                (7, "feature", "main", ["human", "unknown"], "unknown contributors"),
                (8, "mixed", "main", ["openai", "anthropic"], "both model families"),
                (9, "release-fix", "release", ["human"], "never retargets")):
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
            self.assertIn(CI_FAILED.format("pending"), report["limitations"])
            self.github.check_state = "failure"
            run = self.tick_raising(run)
            self.assertNotEqual(run["stage"], "ready")
            self.assertTrue(self.github.pulls[7]["draft"])
            report = self.report(run)
            self.assertEqual(([c["state"] for c in report["ci_checks"]], report["current_ci"]["head"]),
                             (["pending", "failure"], head))
            self.assertIn(CI_FAILED.format("failure"), report["limitations"])

    def test_ci_observations_stay_historical_after_movement_or_fresh_validation(self):
        for change in self.scenarios("base", "configuration", "pins"):
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

    def test_movement_around_readiness_writes_lookup_or_drafting_is_never_saved_as_ready(self):
        for method, trigger, kind in self.scenarios(
                ("comment", "-review-", "head"), ("status", "success", "base"), ("comment", "-ready", "head"),
                ("ci", "", "local"), ("ci", "", "dirty"),
                *(("run", "status", k) for k in ("head", "base", "configuration", "local", "dirty"))):
            head, run = self.readiness_run()
            if method == "run":
                # Custom wording makes the ready comment a model draft.
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
            self.assertEqual((self.github.pulls[7]["draft"], run["ci_checks"][-1]["readiness_changed"]),
                             (not ready, ready))
            if ready:
                limitations = self.report(run)["limitations"]
                self.assertIn(STALE_READY.format(head), limitations)
                self.assertFalse(any("did not change draft" in item for item in limitations))
                continue
            self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
            self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
            if method != "status":
                self.assert_no_success()
            if method in {"ci", "run"}:
                self.assertFalse([c for c in comment.call_args_list if c.args[2].endswith(("-ready", head))])
            if kind in {"local", "dirty"}:
                self.assertTrue(run["pending_contribution"])
                self.assertIn(LOCAL_CHANGE, run["evidence_invalidations"][-1]["reason"])

    def test_moved_inputs_withhold_repair_adoption_writes(self):
        kinds = ("head", "base", "identity", "configuration")
        for point, kind in self.scenarios(*zip(INTERRUPTIONS, kinds), *((None, k) for k in kinds)):
            run = self.repair_handoff(grants=["github"])
            head, repair = run["sha"], self.commit("feature", "feature", "repair.txt", "repair\n", "Repair")
            adopt = lambda: self.team.adopt(run["id"], ["human"])
            if point:
                self.interrupt(point, adopt)
                self.change(kind)
                adopt()
            else:
                with self.writes("status", lambda state: state == "pending", lambda: self.change(kind)):
                    adopt()
            run = self.reload(run)
            self.assertEqual([(i["type"], i["evidence"]) for i in run["unpublished_evidence"]],
                             [("status", repair)] * bool(point) + [("comment", repair)])
            self.assertEqual(([k for _, k in self.github.comments if "-adopt-" in k],
                              (repair, "pending") in self.github.statuses), ([], not point))
            self.assertEqual((run["outbox"], run["adoptions"][-1]["head"], self.head_of(self.store.workspace(run))),
                             ([], repair, repair))
            self.assertEqual([self.head_of(p) for p in self.store.run_root(run).glob("author-preserved-*")], [head])
            self.assert_fields(run, stage="stopped" if kind == "configuration" else "stale", validated_sha=None)

    def test_pr_show_verifies_the_complete_binding_without_a_tick(self):
        for change in self.scenarios("head", "base", "identity", "configuration", "pins", "local", "worker", "ready"):
            if change == "ready":
                head, run = self.readiness_run()
                run = self.ticks(run)
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
            self.assertEqual(self.reload(run), run)

    def test_concurrent_head_change_requires_deliberate_update(self):
        _, run = self.under_review()
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run)
        self.assertEqual((run["stage"], self.agents.calls), ("stale", []))
        self.assertIn("pr update", run["error"])
        self.refuses("pr update", self.team.refresh, run["id"])
        run = self.update(run)
        self.assert_awaiting(run, sha=external, validated_sha=None)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir() and run["evidence_invalidations"])
        self.refuses("unchanged", self.update, run)
        self.assert_reviewed(self.revalidate(run), external)
        self.assertEqual(self.remote_head(), external)

    def test_base_movement_after_stopped_review_retires_evidence_and_is_never_merged(self):
        old = self.remote_head("main")
        head, run = self.stopped_review()
        base = self.advance_base()
        self.assertEqual(self.github.pr(None, 7)["base"]["snapshot_sha"], old)
        run = self.ticks(run)
        self.assert_fields(run, stage="stale", reviewed_sha=None, review_record=None)
        self.assertIn("never merged implicitly", run["error"])
        self.assert_retired(self.report(run), head, old, "PR base changed")
        self.assertEqual(len(self.agents.calls), 1)
        run = self.update(run)
        self.assert_fields(run, sha=head, base_sha=base, reviewed_sha=None)
        self.assertEqual(self.remote_head(), head)
        self.assertFalse(run["adopted_pr"]["base_contained"])
        self.assert_limited(self.report(run), "base was not merged")
        run = self.revalidate(run)
        self.assert_reviewed(run, head, base_sha=base)
        self.assertTrue(self.report(run)["current_evidence"])

    def test_head_moved_before_publication_is_never_overwritten(self):
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

        def lost_response(cwd, *args):
            result = self.local_git(cwd, *args)
            if "push" in args:
                self.assertFalse(any(str(a).startswith("+") or a == "--force" for a in args))
                self.assertIn(f"{run['sha']}:refs/heads/feature", args)
                raise TeamError("Simulated lost push response")
            return result

        with patch("agent_team.coordinator.git", side_effect=lost_response):
            run = self.ticks(run)
        self.assert_fields(run, stage="blocked", pending_push_sha=run["sha"])
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_reviewed(run, run["sha"], pending_push_sha=None, outbox=[])
        self.assertEqual((self.remote_head(), run["adopted_pr"]["pushed"]), (run["sha"], [run["sha"]]))
        self.assert_status(run["sha"], "pending", PUSHED)

    def test_head_advanced_during_permission_lookup_is_never_pushed(self):
        run = self.at_publish()
        head = self.remote_head()
        with self.moving(self.github, "push_access", lambda: self.local_commit(run)):
            run = self.ticks(run)
        self.assert_awaiting(run, validated_sha=None, pending_push_sha=None)
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (head, []))
        self.assertTrue(run["pending_contribution"])
        self.assertIn(LOCAL_CHANGE, run["evidence_invalidations"][-1]["reason"])

    def test_local_change_during_review_or_after_readiness_retires_evidence_until_declared(self):
        reviews = [("review", k, g) for k in ("local", "dirty") for g in ([], ["github"])]
        for when, kind, grants in self.scenarios(*reviews, ("ready", "local", None), ("ready", "dirty", None)):
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
            self.refuses("contributor declarations", self.select, run, ["validate", "review"])
            if kind == "local":
                run = self.revalidate(run, contributors=["human"])
                self.assert_reviewed(run, self.head_of(self.store.workspace(run)))
                self.assertNotEqual(run["reviewed_sha"], head)

    def test_interrupted_adoption_keeps_explicit_roles(self):
        for trailer, author, reviewer in self.scenarios(("openai", "codex", "claude"), ("anthropic", "claude", "codex")):
            if trailer == "openai":
                # Rotation would now pick the contributing family as reviewer.
                self.store.save(self.store.create(self.project, self.github.items[0]), stage="closed")
            self.open_pr(message=f"Add feature\n\nAgent-Family: {trailer}")
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

    def test_validation_reports_commands_omitted_after_early_failure(self):
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

    def test_queued_review_comment_is_retried_or_withheld_after_changes(self):
        reasons = {"configuration": "Validation configuration changed", "pins": "Companion pins changed"}
        for change in self.scenarios(None, "base", "configuration", "pins"):
            head, run = self.under_review(grants=["github"])
            with patch.object(self.github, "comment", side_effect=TeamError("GitHub unavailable")), \
                    self.assertRaises(TeamError):
                self.ticks(run)
            run = self.reload(run)
            self.assertEqual((run["stage"], len(run["outbox"])), ("stopped", 1))
            self.change(change)
            with self.pinned(change):
                run = self.ticks(run)
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
            self.refuses("validat", self.select, run, ["review"])
            self.assertEqual(len(self.agents.calls), 1)

    def test_movement_during_review_retires_evidence(self):
        for case in self.scenarios("head", "local", "handoff", "closed", "merged"):
            if case == "handoff":
                self.configure(max_revisions=0)
            base, rejected = self.remote_head("main"), case in {"head", "handoff"}
            closing = case in {"closed", "merged"}
            head, run = self.under_review(rejected, grants=[] if case == "local" else ["github"])
            run = self.tick_moving_during_review(
                run, (lambda: self.github.pulls[7].update(state="closed", merged=case == "merged")) if closing else None)
            report = self.report(run)
            self.assertFalse(report["current_evidence"])
            self.assert_review(report["review"], head, False, "changes_requested" if rejected else "pass")
            self.assertEqual(self.github.comments, {})
            if case == "head":
                self.assert_contains(run["error"], "PR head moved", "before review evidence was published")
                self.assertNotIn((head, "failure"), self.github.statuses)
                self.assertEqual((run["stage"], run["outbox"]), ("stale", []))
                self.assertEqual({i["evidence"] for i in run["unpublished_evidence"]}, {head})
                self.assertEqual(len(report["unpublished_evidence"]), 2)
                self.assert_limited(report, "was not published")
                run = self.ticks(run, 2)
                self.assertEqual((run["stage"], self.github.comments, len(self.agents.calls)), ("stale", {}, 1))
                self.assert_awaiting(self.update(run))
            elif case == "local":
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
                self.assert_fields(run, stage="stale", handoffs=None, round=0, outbox=[])
                self.assertEqual((run.get("rejected_shas", []), self.github.statuses), ([], []))
                self.assertIn(self.marker(run, head)[1], [i.get("marker") for i in run["unpublished_evidence"]])
                self.assertIn("PR head moved", run["evidence_retired"]["reason"])
                self.refuses("handoff", self.team.decide, run["id"], "repair")
            else:
                self.assert_fields(run, stage=case, reviewed_sha=None, outbox=[])
                self.assert_retired(report, head, run["base_sha"], "")
                self.assertEqual(self.github.pulls[7]["state"], "closed")

    def test_movement_during_first_outbox_write_withholds_later_evidence(self):
        head, run = self.under_review(True, grants=["github"])
        with self.writes("comment", lambda key: "-review-" in key, self.push_external):
            run = self.ticks(run)
        self.assertIn(self.marker(run, head), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual((run["stage"], run["outbox"], len(self.agents.calls)), ("stale", [], 1))
        self.assertEqual([i["type"] for i in run["unpublished_evidence"]], ["status"])
        self.assertEqual(run["evidence_invalidations"][-1]["head"], head)
        for reason in (run["error"], run["evidence_invalidations"][-1]["reason"]):
            self.assertIn("PR head moved", reason)

    def test_rejecting_review_with_moved_inputs_is_history_only(self):
        reasons = {"configuration": "Validation configuration changed", "local": LOCAL_CHANGE, "dirty": LOCAL_CHANGE,
                   "head": "PR head moved", "base": "PR base changed"}
        for kind in self.scenarios(*reasons):
            self.configure(max_revisions=0)
            base = self.remote_head("main")
            head, run = self.under_review(True, grants=["github"])
            run = self.tick_moving_during_review(run, lambda: self.change(kind, run))
            self.assert_fields(run, round=0, handoffs=None, revision_history=None, review_record=None, outbox=[],
                               stage="stale" if kind in {"head", "base"} else "stopped")
            self.assertEqual((run.get("rejected_shas", []), bool(run.get("needs_revision"))), ([], False))
            self.assertEqual((self.github.comments, self.github.statuses), ({}, []))
            self.assertEqual({i["type"] for i in run["unpublished_evidence"]}, {"comment", "status"})
            self.assert_retired(self.report(run), head, base, reasons[kind], verdict="changes_requested")
            if kind == "configuration":
                self.agents.reject = False
                self.assert_reviewed(self.revalidate(run), head)

    def test_close_refuses_interrupted_swap_and_recovery_keeps_closed_runs(self):
        for updating, point in self.scenarios(*((u, p) for u in (True, False) for p in INTERRUPTIONS)):
            run, _, external, recover = self.swap_fixture(updating)
            self.interrupt(point, recover)
            stage = self.reload(run)["stage"]
            self.refuses("interrupted", self.command, "close", run["id"])
            self.assertEqual(self.reload(run)["stage"], stage)
            if point == "rename-2":
                # Closed outside the CLI.
                self.store.save(self.reload(run), stage="closed")
            run = recover()
            self.assert_fields(run, pending_swap=None, sha=external, stage="closed" if point == "rename-2" else "stopped")
            self.command("close", run["id"])
            self.assert_fields(self.reload(run), stage="closed", pending_swap=None)

    def test_post_push_status_survives_failures_crashes_and_movement(self):
        for case in self.scenarios("failed", "crash", "moved"):
            run = self.at_publish()
            real_status, real_save = self.github.status, self.store.save

            def status(repo, sha, state, description):
                if description.startswith("Revision pushed"):
                    raise TeamError("GitHub unavailable")
                return real_status(repo, sha, state, description)

            def save(record, **changes):
                real_save(record, **changes)
                if "published_sha" in changes and changes.get("outbox"):
                    raise Interrupted()

            def pushing(cwd, *args):
                result = self.local_git(cwd, *args)
                if "push" in args:
                    self.push_external()
                return result

            context = {"failed": patch.object(self.github, "status", side_effect=status),
                       "crash": patch.object(self.store, "save", side_effect=save),
                       "moved": patch("agent_team.coordinator.git", side_effect=pushing)}[case]
            with context:
                try:
                    self.ticks(run)
                except (Interrupted, TeamError):
                    pass
            run = self.reload(run)
            pushed = run["published_sha"]
            self.assertEqual((self.remote_head() == pushed, run["adopted_pr"]["pushed"]), (case != "moved", [pushed]))
            descriptions = [(s, d) for s, _, d in self.github.status_descriptions if s == pushed]
            if case == "moved":
                self.assertEqual((run["stage"], run["outbox"], descriptions), ("stale", [], []))
                self.assertEqual([i["description"] for i in run["unpublished_evidence"]], [PUSHED])
                continue
            self.ticks(self.team.resume(run["id"]) if case == "failed" else run)
            if case == "crash":
                self.team.resume(run["id"])
            run = self.ticks(run, 2)
            self.assert_reviewed(run, pushed, outbox=[])
            descriptions = [d for s, _, d in self.github.status_descriptions if s == pushed]
            self.assertIn(PUSHED, descriptions)
            self.assertNotEqual(descriptions[-1], "Coordinator blocked; maintainer action needed")

    def test_deliberate_update_snapshots_review(self):
        base = self.remote_head("main")
        head, run = self.stopped_review()
        external = self.push_external()
        run = self.update(run)
        self.assert_fields(run, sha=external, review_record=None)
        report = self.report(run)
        historical = self.assert_retired(report, head, base, DELIBERATE, reviewed=head)
        self.assert_review(historical["review"], head, False, agent=run["reviewer"])
        self.assert_review(report["review"], head, False)

    def test_persisted_rejection_with_changed_inputs_is_retired_on_recovery(self):
        updates = [(c, "update") for c in ("head", "base", "identity", "configuration", "local")]
        for change, entry in self.scenarios(*updates, ("local", "resume")):
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
            self.assertEqual((run.get("rejected_shas", []), self.roles()), ([], ["review"]))
            self.assertEqual([(h["head"], h["verdict"]) for h in self.report(run)["historical_evidence"]
                              if h["review"]], [(head, "changes_requested")])
            if change in {"head", "base"}:
                self.assert_awaiting(run)

    def test_review_history_keeps_a_rejection_followed_by_validation_failure(self):
        self.configure(max_revisions=1, tests=["! grep -q fixed feature.txt"])
        head = self.open_pr(reject=1)
        run = self.until_handoff(self.editing())
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
        head = self.open_pr(8, "drift")
        run = self.reviewed("8", count=2)
        (self.store.workspace(run) / "stray.txt").write_text("operator edit\n")
        run = self.ticks(run)
        self.assert_awaiting(run, validated_sha=None)
        self.assert_retired(self.report(run), head, run["base_sha"], DRIFT, verdict=None, validated=head)
        self.assertEqual(self.roles(), ["review", "review"])

    def test_same_sha_head_identity_change_retires_evidence(self):
        for change in self.scenarios("identity", "repository"):
            head, run = self.stopped_review(grants=["github"])
            self.change(change)
            run = self.ticks(run)
            self.assert_fields(run, stage="stale", validated_sha=None, reviewed_sha=None)
            self.assertIn("head repository or branch changed", run["error"])
            report = self.report(run)
            self.assertFalse(report["independent_review_success"])
            self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
            self.assert_status(head, "pending", "PR head changed; review invalidated")
            self.refuses("adopt the PR again", self.update, run)

    def test_repair_adoption_refuses_changed_head_identity(self):
        for change in self.scenarios("repository", "identity"):
            run = self.repair_handoff()
            before = self.head_of(self.store.workspace(run))
            self.change(change)
            self.refuses("head repository or branch changed", self.team.adopt, run["id"], ["human"])
            after = self.reload(run)
            self.assert_fields(after, stage="repair", adoptions=None, adopted_pr=run["adopted_pr"])
            self.assertEqual(self.head_of(self.store.workspace(after)), before)
            self.assertEqual(list(self.store.run_root(after).glob("refresh-*")), [])

    def test_continuation_after_update_stops_on_rejection_without_revision(self):
        head = self.open_pr()
        run = self.ticks(self.writable(), 3)
        self.assert_reviewed(run, head)
        external = self.push_external()
        self.assertEqual(self.ticks(run)["stage"], "stale")
        self.update(run)
        run = self.select(run, ["validate", "review"])
        self.assert_fields(run, pr_followup=None, released_pr_followup=FOLLOWUP)
        self.agents.reject = True
        run = self.ticks(run, 4)
        self.assert_awaiting(run, "implement", sha=external)
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual(self.roles(), ["review", "review"])
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (external, []))

    def test_failing_head_is_reviewed_before_any_edit(self):
        for mode, grants in self.scenarios(("revise", ALL), ("review", ["github"])):
            head = self.open_pr()
            self.configure(tests=["grep -q fixed feature.txt"])
            self.github.permissions["example/demo"] = True
            run = self.ticks(self.adopt_pr(mode, grants=grants), 2)
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
                self.assertEqual(self.agents.calls, self.revised_calls(run))
                self.assert_reviewed(run, run["sha"], validated_sha=run["sha"])
                self.assert_pushed_on(run, head)
                continue
            self.assert_awaiting(run, "implement")
            report = self.report(self.assert_stop_boundary(run, head))
            review = report["review"]
            self.assert_review(review, head, True)
            self.assert_fields(review, summary=self.agents.summary, findings=[], validation_failed=True,
                               candidate_verdict="changes_requested")
            self.assertEqual(review["candidate_findings"][0]["severity"], "validation")
            self.assertFalse(report["validation"]["passed_for_candidate"] or report["independent_review_success"])

    def test_unresolved_trailers_in_external_repair_withhold_independence(self):
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
            self.assertEqual(run["contributors"], sorted({"human", *declared}))
            self.refuses_revision("Independent review cannot be established", run)
            self.agents.reject = False
            run = self.revalidate(run, contributors=declared)
            self.assert_withheld(run, external, unresolved)
            self.assertNotIn("implement", self.roles())

    def test_unresolved_trailers_in_local_continuation_withhold_independence(self):
        head = self.open_pr()
        run = self.ticks(self.editing(), 3)
        self.assert_reviewed(run, head)
        local = self.local_commit(run, "Local change\n\nAgent-Family: gemini")
        run = self.select(run, ["validate", "review"], contributors=["human"])
        self.assertFalse(run["independence"]["established"])
        self.assert_fields(run, unresolved_trailers=["gemini"], reviewed_sha=None)
        self.assertEqual(run["adopted_pr"]["unresolved_trailers"], ["gemini"])
        run = self.ticks(run, 2)
        self.assert_withheld(run, local)
        self.assertEqual(self.remote_head(), head)

    def test_interrupted_update_or_repair_adoption_recovers_recorded_inputs(self):
        commands = [(c, p) for c in ("pr update RUN_ID", "adopt RUN_ID") for p in INTERRUPTIONS]
        for command, point in self.scenarios(*commands):
            updating = command == "pr update RUN_ID"
            run, head, external, recover = self.swap_fixture(updating)
            self.interrupt(point, recover)
            stored = self.reload(run)
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
            self.assertEqual((self.head_of(self.store.workspace(run)), run["adopted_pr"]["head_sha"]),
                             (external, external))
            preserved = self.store.run_root(run).glob("author-preserved-*")
            self.assertEqual([self.head_of(p) for p in preserved], [head])
            self.assertEqual(self.remote_head(), external if updating else later)
            if updating:
                self.assertEqual((run["evidence_context"]["head"], run["evidence_invalidations"][-1]["reason"]),
                                 (external, DELIBERATE))
                self.assert_reviewed(self.revalidate(run), external)
                continue
            self.assertEqual(([(a["head"], a["declared"]) for a in run["adoptions"]], run["round"]),
                             ([(external, ["human"])], 1))
            run = self.ticks(run)
            self.assertEqual(run["stage"], "stale")
            self.assertIn("agent-team adopt", run["error"])

    def test_supplied_findings_are_revised_with_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=FIX)
        self.assertEqual(run["operations"], FOLLOWUP)
        run = self.ticks(run, 5)
        self.assertEqual((run["stage"], run.get("rejected_shas", [])), ("stopped", []))
        self.assert_contains(self.agents.prompts["implement"], FIX[0], "existing PR #7")
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"], self.remote_head()), (run["sha"],) * 3)
        self.assertTrue(self.github.pulls[7]["draft"])

    def test_findings_mode_counts_its_first_edit_against_the_budget(self):
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

    def test_evidence_is_invalidated_by_configuration_change(self):
        head, run = self.stopped_review()
        self.change("configuration")
        self.refuses("evidence invalidated", self.select, run, ["review"])
        self.assert_fields(self.reload(run), validated_sha=None, reviewed_sha=None)

    def test_exhausted_review_hands_off_and_repair_is_adopted_without_base_merge(self):
        run = self.repair_handoff()
        external = self.push_external()
        self.advance_base()
        run = self.team.adopt(run["id"], ["human"])
        self.assert_awaiting(run, sha=external)
        self.assert_fields(run["adopted_pr"], head_sha=external, base_contained=False)
        self.assertEqual(self.remote_head(), external)

    def test_every_author_entry_needs_a_reserved_unused_round(self):
        for limit in self.scenarios(0, 2):
            self.configure(max_revisions=limit)
            head = self.open_pr()
            run = self.ticks(self.writable(), 3)
            self.assert_reviewed(run, head, round=0)
            for operations in (["implement", "validate", "review"], REVISION):
                self.refuses("No revision round is reserved", self.select, run, operations, ["edit"])
            self.store.save(run, round=limit + 1, reserved_round=limit + 1, needs_revision=True)
            self.refuses(NO_BUDGET, self.select, run, ["implement", "validate"], ["edit"])
            self.assertEqual((self.roles(), self.remote_head()), (["review"], head))

    def test_supplied_findings_stop_on_fresh_unrelated_findings(self):
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

    def test_readoption_carries_prior_provenance_forward(self):
        for first, second, message in self.scenarios(
                (["openai"], ["human"], "cannot review independently"),
                (["human", "unknown"], ["human"], "unknown contributors"),
                (["openai"], ["anthropic"], "both model families")):
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
                self.assert_fields(run, stage="blocked", reviewed_sha=None, review_record=None)
                self.assertIn("exceeds review budget even without context", run["error"])
                self.assertEqual(self.agents.calls, [])
                continue
            self.assert_reviewed(run, head)
            self.assert_fields(run["review_record"]["patch"], format="compact", complete=True, files=2,
                               changed_lines=13, characters=compact, default_characters=default, range=span)
            prompt = self.agents.prompts["review"]
            self.assert_contains(prompt, COMPACT_NOTICE, "-line 50\n+edited 50\n")
            self.assertNotIn("\n line 51\n", prompt)
            self.assertEqual(self.report(run)["review"]["patch"], run["review_record"]["patch"])
            self.assertIn("complete context-free patch", review_comment(head, run["review_record"]))

    def test_cli_parses_existing_pr_operations(self):
        parse = parser().parse_args
        args = parse(["pr", "review", "demo", "7", "--contributor", "unknown"])
        self.assertEqual((args.pr_command, args.pull, args.contributor), ("review", "7", ["unknown"]))
        args = parse(["pr", "findings", "demo", "https://github.com/example/demo/pull/7",
                      "--contributor", "human", "--grant", "edit", "--finding", "Fix the parser"])
        self.assertEqual(args.finding, ["Fix the parser"])
        self.assertEqual(parse(["pr", "update", "RUN", "--contributor", "human"]).run_id, "RUN")
        with self.assertRaises(SystemExit):
            parse(["pr", "review", "demo", "7", "--contributor", "human", "--grant", "readiness"])
        self.assertEqual(parse(["adopt", "RUN", "--contributor", "unknown"]).contributor, ["unknown"])
        self.refuses("Unknown contributor", self.team.select, "demo", ["validate"], [], task="Validate", ref="main",
                     contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
