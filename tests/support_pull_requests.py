"""Reusable offline pull-request fixture: a fake GitHub API over a local bare remote, isolated Git
working copies, scenario labels, and movement, write, and crash hooks. It needs no existing-PR
feature API, so it works on any coordinator that accepts a GitHub provider."""
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from agent_team.coordinator import Coordinator
from agent_team.process import git, TeamError

# A module import, so discovery does not rerun WorkflowTests here.
from tests import test_coordinator
from tests.test_coordinator import FakeGitHub

COMMIT = ["-c", "user.name=Human", "-c", "user.email=human@example.invalid", "-c", "commit.gpgsign=false"]
FORK = "someone/demo-fork"


class Interrupted(Exception):
    pass


def scenarios(*cases):
    """Run the test once per labeled case, each in a fresh fixture."""
    def decorate(check):
        def test(self):
            for case in cases:
                with self.subTest(case=case):
                    self.tearDown()
                    self.setUp()
                    check(self, *case if isinstance(case, tuple) else (case,))
        return test
    return decorate


def trailed(message, *families):
    trailers = "\n".join(f"Agent-Family: {family}" for family in families if family)
    return f"{message}\n\n{trailers}" if trailers else message


class PullGitHub(FakeGitHub):
    """Tracked pulls are served as raw API snapshots; feature tests may layer production parsing on top."""

    def __init__(self, remote):
        super().__init__(remote)
        self.pulls = {}
        self.permissions = {}

    def pr(self, repo, number):
        return self.api(f"repos/{repo}/pulls/{number}") if number in self.pulls else super().pr(repo, number)

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

    def mark_ready(self, repo, number):
        if number not in self.pulls:
            return super().mark_ready(repo, number)
        self.pulls[number]["draft"] = False


class PullRequestFixture(unittest.TestCase):
    """Defines no tests. `provider` builds the GitHub stand-in for each fresh fixture."""
    provider = PullGitHub
    tearDown = test_coordinator.WorkflowTests.tearDown

    def setUp(self):
        test_coordinator.WorkflowTests.setUp(self)
        self.source = self.root / "source"
        self.github = self.provider(self.remote)
        self.team = Coordinator(self.store, self.github, self.agents)
        self.git_patch.stop()
        self.git_patch = patch("agent_team.coordinator.git", side_effect=self.local_git)
        self.git_patch.start()

    def local_git(self, cwd, *args):
        args = list(args)
        if "push" in args:
            args[args.index("push") + 1] = str(self.remote)
        if "fetch" in args:
            args = [str(self.remote) if str(a).startswith("https://github.com/") else a for a in args]
            args = [f"refs/heads/{self.pull(int(m[1]))['branch']}"
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

    def open_pr(self, number=7, branch="feature", head_repo="example/demo", base="main", families=(),
                user="octocat", maintainer_can_modify=False, reject=None):
        if reject is not None:
            self.agents.reject = reject
        self.github.pulls[number] = dict(
            title="Existing feature", body="Human description", state="open", merged=False, draft=True, user=user,
            branch=branch, head_repo=head_repo, base=base, frozen_base=self.remote_head(base),
            maintainer_can_modify=maintainer_can_modify)
        return self.commit(branch, base, "feature.txt", f"external feature {branch}\n",
                           trailed("Add feature", *families))

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
            self.pull()["branch"] = "other-branch"
        elif kind == "repository":
            self.pull()["head_repo"] = FORK
        elif kind == "configuration":
            self.configure(tests=["true"])
        elif kind == "dirty":
            (self.store.workspace(run) / "local.txt").write_text("operator edit\n")
        elif kind in {"head", "base", "local"}:
            return {"head": self.push_external, "base": self.advance_base,
                    "local": lambda: self.local_commit(run)}[kind]()

    def configure(self, **settings):
        self.store.update_project("demo", **settings)

    def pull(self, number=7):
        return self.github.pulls[number]

    def reload(self, run):
        return self.store.get(run["id"])

    def head_of(self, path):
        return git(path, "rev-parse", "HEAD")

    def workspace_head(self, run):
        return self.head_of(self.store.workspace(run))

    def preserved(self, run):
        return [self.head_of(p) for p in self.store.run_root(run).glob("author-preserved-*")]

    def refuses(self, message, action, *args, **kwargs):
        with self.assertRaisesRegex(TeamError, message):
            action(*args, **kwargs)

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

    def crash(self):
        raise Interrupted()

    def interrupt(self, point, action):
        real_rename, real_save, renames = Path.rename, self.store.save, []

        def rename(path, target):
            result = real_rename(path, target)
            renames.append(target)
            if point == f"rename-{len(renames)}":
                raise Interrupted()
            return result

        def save(record, **changes):
            if (point == "final-save" and "pending_swap" in changes and changes["pending_swap"] is None
                    or point == "metadata" and "git_metadata" in changes):
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

    def assert_fields(self, record, **expected):
        self.assertEqual({k: record.get(k) for k in expected}, expected)

    def assert_error(self, run, stage, *texts):
        self.assertEqual(run["stage"], stage)
        self.assert_contains(run["error"], *texts)

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
        self.assertTrue(self.pull(number)["draft"])

    def assert_review(self, review, commit, current, verdict="pass", base=None, agent=None):
        self.assertEqual((review["commit"], review["current"], review["verdict"]), (commit, current, verdict))
        if base:
            self.assertEqual(review["base"], base)
        if agent:
            self.assertEqual(review["reviewer"]["agent"], agent)
