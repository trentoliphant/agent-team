"""Proofs for the offline pull-request fixture; they use no existing-PR feature API."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_team import coordinator
from agent_team.process import git, TeamError

# Module imports, so discovery collects neither the fixture class nor WorkflowTests here.
from tests import support_pull_requests as support
from tests import test_coordinator
from tests.support_pull_requests import COMMIT, FORK, Interrupted, scenarios, trailed


class Target:
    def __init__(self):
        self.log = []

    def call(self, key, fail=False):
        self.log.append(("call", key))
        if fail:
            raise TeamError("failed")
        return key


class FixtureProofTests(support.PullRequestFixture):
    def test_fixture_defines_no_tests_and_borrows_no_workflow_tests(self):
        self.assertFalse(issubclass(support.PullRequestFixture, test_coordinator.WorkflowTests))
        self.assertEqual([n for n in dir(support.PullRequestFixture) if n.startswith("test")], [])
        self.assertNotIn("push_access", vars(support.PullGitHub))
        self.assertIs(self.provider, support.PullGitHub)
        # Discovery collects only this module's own proofs, never a duplicate suite.
        cases = [n for n, v in globals().items() if isinstance(v, type) and issubclass(v, unittest.TestCase)]
        self.assertEqual(cases, ["FixtureProofTests"])

    def test_scenarios_rebuild_a_fresh_fixture_per_case(self):
        seen = []

        def check(test, label, number=0):
            self.assertEqual((test.github.pulls, test.github.statuses, test.agents.calls), ({}, [], []))
            seen.append((label, number, test.root, test.github))
            test.open_pr()
            test.github.status("example/demo", "sha", "success", "Done")

        scenarios("plain", ("pair", 2))(check)(self)
        (first, _, old_root, old_github), (second, number, root, github) = seen
        self.assertEqual((first, second, number), ("plain", "pair", 2))
        self.assertNotEqual(old_root, root)
        self.assertFalse(old_root.exists())
        self.assertTrue(root.exists())
        self.assertIsNot(old_github, github)
        self.assertIs(self.github, github)
        self.assertIs(self.team.github, github)

    def test_provider_factory_is_injectable(self):
        class Custom(support.PullGitHub):
            def push_access(self, project, pr):
                return {"allowed": False, "reason": "custom"}

        self.tearDown()
        self.provider = Custom
        self.setUp()
        self.assertIsInstance(self.github, Custom)
        self.assertIs(self.team.github, self.github)
        self.assertEqual(self.github.push_access(self.project, {})["reason"], "custom")

    def test_fake_api_freezes_base_and_follows_head_and_branch(self):
        base = self.remote_head("main")
        head = self.open_pr(families=("openai", None), user="someone", maintainer_can_modify=True)
        snapshot = self.github.api("repos/example/demo/pulls/7")
        self.assertEqual(self.github.pr("example/demo", 7), snapshot)
        self.assert_fields(snapshot, number=7, state="open", merged=False, draft=True, maintainer_can_modify=True,
                           title="Existing feature", body="Human description")
        self.assertEqual((snapshot["user"]["login"], snapshot["head"]["sha"], snapshot["head"]["ref"],
                          snapshot["head"]["repo"]["full_name"], snapshot["base"]),
                         ("someone", head, "feature", "example/demo", {"ref": "main", "sha": base}))
        self.assertIn("Agent-Family: openai", git(self.remote, "log", "-1", "--format=%B", head))
        self.assertEqual((trailed("Add"), trailed("Add", None), trailed("Add", "a", "b")),
                         ("Add", "Add", "Add\n\nAgent-Family: a\nAgent-Family: b"))
        moved = self.advance_base()
        self.assertEqual(self.github.api("repos/example/demo/pulls/7")["base"]["sha"], base)
        self.assertEqual(self.github.api("repos/example/demo/git/ref/heads/main")["object"]["sha"], moved)
        external = self.push_external()
        self.assertEqual((git(self.remote, "rev-parse", f"{external}^"), self.remote_head()), (head, external))
        self.assertEqual(self.github.api("repos/example/demo/pulls/7")["head"]["sha"], external)
        git(self.remote, "branch", "renamed", head)
        self.pull()["branch"], self.pull()["head_repo"] = "renamed", FORK
        self.assertEqual(self.github.api("repos/example/demo/pulls/7")["head"],
                         {"sha": head, "ref": "renamed", "repo": {"full_name": FORK}})

    def test_fake_permissions_and_readiness_stay_per_pull(self):
        self.open_pr()
        self.open_pr(8, "other")
        self.assertFalse(self.github.repo(FORK)["permissions"]["push"])
        self.github.permissions[FORK] = True
        self.assertEqual(self.github.repo(FORK), {"full_name": FORK, "permissions": {"push": True}})
        self.github.mark_ready("example/demo", 8)
        self.assertEqual((self.pull()["draft"], self.pull(8)["draft"], self.github.pull), (True, False, None))
        self.assert_untouched(self.remote_head(), 7, "feature")

    def test_local_git_isolates_pushes_and_pull_fetches(self):
        head = self.open_pr(9, "pr-nine")
        work = self.root / "work"
        git(self.root, "clone", "--quiet", str(self.remote), str(work))
        coordinator.git(work, "fetch", "https://github.com/example/demo.git", "refs/pull/9/head")
        self.assertEqual(git(work, "rev-parse", "FETCH_HEAD"), head)
        git(work, "checkout", "-B", "pushed", "FETCH_HEAD")
        (work / "pushed.txt").write_text("pushed\n")
        git(work, "add", ".")
        git(work, *COMMIT, "commit", "-m", "Pushed")
        coordinator.git(work, "push", "https://github.com/example/demo.git", "HEAD:refs/heads/pushed")
        self.assertEqual(self.remote_head("pushed"), self.head_of(work))
        self.assertEqual(self.remote_head("pr-nine"), head)
        self.assertEqual(git(self.source, "rev-parse", "--abbrev-ref", "HEAD"), "pr-nine")

    def test_bootstrap_and_clones_ignore_hostile_git_environment_and_configuration(self):
        hostile = Path(self.enterContext(tempfile.TemporaryDirectory()))
        elsewhere, marker = hostile / "elsewhere", hostile / "hooked"
        hook = f"#!/bin/sh\necho hooked >> '{marker}'\n"
        for hooks in (hostile / "hooks", hostile / "template" / "hooks"):
            hooks.mkdir(parents=True)
            for name in ("post-checkout", "pre-commit", "post-commit", "pre-push", "reference-transaction"):
                (hooks / name).write_text(hook)
                (hooks / name).chmod(0o755)
        elsewhere.mkdir()
        # A rewrite of every absolute path to loopback HTTP: if honored, clones fail instead of staying local.
        config = (f'[url "http://127.0.0.1:9/"]\n\tinsteadOf = /\n[init]\n\ttemplateDir = {hostile / "template"}\n'
                  f'[core]\n\thooksPath = {hostile / "hooks"}\n[protocol]\n\tallow = always\n')
        for path in (hostile / "config", hostile / "home" / ".gitconfig", hostile / "xdg" / "git" / "config"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(config)
        env = {"GIT_DIR": str(elsewhere), "GIT_WORK_TREE": str(elsewhere), "GIT_TEMPLATE_DIR": str(hostile / "template"),
               "GIT_CONFIG_GLOBAL": str(hostile / "config"), "GIT_CONFIG_SYSTEM": str(hostile / "config"),
               "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(hostile / "hooks"),
               "HOME": str(hostile / "home"), "XDG_CONFIG_HOME": str(hostile / "xdg")}
        with patch.dict(os.environ, env):
            self.tearDown()
            self.setUp()
            head = self.open_pr()
            work = self.root / "work"
            coordinator.clone_repository("example/demo", work, "feature", 60)
            coordinator.git(work, "fetch", "https://github.com/example/demo.git", "refs/pull/7/head")
            coordinator.git(work, "push", "https://github.com/example/demo.git", "HEAD:refs/heads/copied")
        self.assertEqual((git(work, "rev-parse", "HEAD"), git(work, "rev-parse", "FETCH_HEAD")), (head, head))
        self.assertEqual((self.remote_head(), self.remote_head("copied")), (head, head))
        self.assertEqual(git(self.remote, "rev-parse", "--is-bare-repository"), "true")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertFalse(marker.exists())
        for hooks in (self.source / ".git" / "hooks", work / ".git" / "hooks", self.remote / "hooks"):
            self.assertFalse((hooks / "post-checkout").exists(), hooks)

    def test_fixture_git_reaches_only_local_files(self):
        work = self.root / "work"
        self.local_clone("example/demo", work, "main", 60)
        self.assertEqual(self.head_of(work), self.remote_head("main"))
        for url in ("http://127.0.0.1:9/demo.git", "git://127.0.0.1:9/demo.git"):
            with self.subTest(url=url):
                self.refuses("not allowed", coordinator.git, work, "fetch", url, "main")
                self.refuses("not allowed", support.isolated, ["git", "clone", url, str(self.root / "net")])
                self.refuses("not allowed", support.local, work, "fetch", url, "main")
                self.refuses("not allowed", support.local, work, "push", url, "HEAD:refs/heads/x")

    def test_moving_runs_callbacks_before_or_after_including_failed_calls(self):
        target = Target()
        move = lambda: target.log.append(("move",))
        hit = lambda key, *a: key == "hit"
        with self.moving(target, "call", move, hit):
            self.assertEqual((target.call("miss"), target.call("hit")), ("miss", "hit"))
            with self.assertRaises(TeamError):
                target.call("hit", True)
        self.assertEqual(target.log, [("call", "miss"), ("move",), ("call", "hit"), ("move",), ("call", "hit")])
        target.log.clear()
        with self.moving(target, "call", move, hit, after=True):
            self.assertEqual(target.call("hit"), "hit")
            with self.assertRaises(TeamError):
                target.call("hit", True)
        # A failed call never reaches its after-callback.
        self.assertEqual(target.log, [("call", "hit"), ("move",), ("call", "hit")])
        self.assertEqual(target.call("hit"), "hit")
        self.assertEqual(len(target.log), 4)

    def test_writes_and_crash_fire_after_the_selected_write_lands(self):
        observed = []
        with self.writes("comment", lambda key: key == "marked", lambda: observed.append(dict(self.github.comments))):
            self.github.comment("example/demo", 7, "other", "first")
            self.github.comment("example/demo", 7, "marked", "second")
        self.assertEqual(observed, [{(7, "other"): "first", (7, "marked"): "second"}])
        with self.writes("status", lambda state: state == "success", self.crash), self.assertRaises(Interrupted):
            self.github.status("example/demo", "sha", "pending", "Waiting")
            self.github.status("example/demo", "sha", "success", "Done")
            self.github.status("example/demo", "sha", "failure", "Never")
        self.assertEqual(self.github.statuses, [("sha", "pending"), ("sha", "success")])
        self.assert_status("sha", "success", "Done")

        def refused():
            raise TeamError("Refused here")

        self.refuses("Refused", refused)
        with self.assertRaises(AssertionError):
            self.refuses("Refused", lambda: None)

    def test_interrupt_stops_renames_in_order_after_the_selected_one(self):
        def renaming():
            for name in "abc":
                (self.root / name).mkdir()
                (self.root / name).rename(self.root / f"{name}-moved")

        for point, done in (("rename-1", "a"), ("rename-2", "ab")):
            with self.subTest(point=point):
                self.tearDown()
                self.setUp()
                self.interrupt(point, renaming)
                self.assertEqual(sorted(p.name for p in self.root.glob("?-moved")), [f"{n}-moved" for n in done])
        self.tearDown()
        self.setUp()
        with self.assertRaises(AssertionError):
            self.interrupt("rename-4", renaming)

    def test_interrupt_save_points_persist_or_skip_exactly_one_write(self):
        run = self.store.create(self.project, self.github.items[0])

        def journal():
            self.store.save(run, pending_swap={"command": "swap"})
            self.store.save(run, stage="after")

        self.interrupt("journal", journal)
        stored = self.reload(run)
        self.assertEqual(stored["pending_swap"], {"command": "swap"})
        self.assertNotEqual(stored["stage"], "after")
        self.interrupt("final-save", lambda: self.store.save(self.reload(run), pending_swap=None))
        self.assertEqual(self.reload(run)["pending_swap"], {"command": "swap"})
        self.interrupt("metadata", lambda: self.store.save(self.reload(run), git_metadata={"x": 1}))
        self.assertNotIn("git_metadata", self.reload(run))
        self.store.save(self.reload(run), pending_swap=None)
        self.assertIsNone(self.reload(run)["pending_swap"])

    def test_tick_helpers_and_generic_assertions(self):
        stored = self.store.create(self.project, self.github.items[0])
        calls = []

        def tick(project, run_id):
            calls.append((project, run_id))
            if len(calls) == 3:
                raise TeamError("blocked")
            return dict(stored, stage="handoff" if len(calls) in {2, 5} else "next")

        with patch.object(self.team, "tick", side_effect=tick):
            self.assertEqual(self.ticks(stored, 2)["stage"], "handoff")
            self.assertEqual(self.tick_raising(stored), self.reload(stored))
            self.assertEqual(self.until_handoff(dict(stored, stage="other"))["stage"], "handoff")
            self.assertEqual(self.until_handoff(dict(stored, stage="handoff"))["stage"], "handoff")
        self.assertEqual(calls, [("demo", stored["id"])] * 5)
        self.agents.calls += [("claude", "review"), ("codex", "implement")]
        self.assertEqual(self.roles(), ["review", "implement"])
        record = {"stage": "stale", "error": "PR head moved; update", "limitations": ["was not published"]}
        self.assert_fields(record, stage="stale", missing=None)
        self.assert_error(record, "stale", "head moved", "update")
        self.assert_limited(record, "not published")
        self.assert_review({"commit": "c", "current": False, "verdict": "pass", "base": "b",
                            "reviewer": {"agent": "claude"}}, "c", False, base="b", agent="claude")
        self.assert_quiet()
        self.assert_no_success()
        for failing in (lambda: self.assert_fields(record, stage="ready"),
                        lambda: self.assert_limited(record, "absent"),
                        lambda: self.assert_contains("text", "missing")):
            with self.assertRaises(AssertionError):
                failing()


if __name__ == "__main__":
    unittest.main()
