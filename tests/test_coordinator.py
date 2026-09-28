import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_team.agents import Agents, FAMILIES, REVIEW_SCHEMA, subscription_status, validate_report
from agent_team.coordinator import Coordinator
from agent_team.process import execute, git, worker_env, TeamError, QuotaError
from agent_team.state import Store


class FakeGitHub:
    def __init__(self, remote):
        self.remote = remote
        self.items = [{"number": 1, "title": "Add feature", "body": "Add feature.txt",
                       "state": "open", "labels": [{"name": "agent:ready"}]}]
        self.pull = None
        self.comments = {}
        self.statuses = []
        self.creates = 0
        self.check_state = "success"
        self.external_sha = None

    def issues(self, project, ready=True):
        return self.items

    def issue(self, repo, number):
        return next(i for i in self.items if i["number"] == number)

    def comment(self, repo, number, marker, body):
        self.comments[(number, marker)] = body

    def create_pr(self, project, run, body):
        if self.pull is None:
            self.creates += 1
            self.pull = {"number": 7, "state": "open", "draft": True, "merged": False,
                         "branch": run["branch"], "body": body}
        return self.pr(project["repo"], 7)

    def pr(self, repo, number):
        return dict(self.pull, head={"sha": self.external_sha or git(self.remote, "rev-parse", self.pull["branch"])},
                    base={"sha": git(self.remote, "rev-parse", "main"), "ref": "main"})

    def status(self, repo, sha, state, description):
        self.statuses.append((sha, state))

    def ci(self, repo, sha):
        return self.check_state

    def mark_ready(self, repo, number):
        self.pull["draft"] = False


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.reject = False
        self.quota = False

    def run(self, agent, role, prompt, cwd, artifacts, project):
        self.calls.append((agent, role))
        if self.quota:
            self.quota = False
            raise QuotaError("quota exhausted")
        if role == "implement":
            path = Path(cwd) / "feature.txt"
            path.write_text(path.read_text() + "fixed\n" if path.exists() else "feature\n")
            report = {"summary": "Added feature", "limitations": "None"}
        else:
            report = {"verdict": "changes_requested" if self.reject else "pass", "summary": "Reviewed",
                      "findings": [{"severity": "high", "location": "feature.txt:1", "evidence": "Bug",
                                    "request": "Fix"}] if self.reject else []}
            self.reject = False
        return {"agent": agent, "family": FAMILIES[agent], "cli_version": "test", "requested_model": "test",
                "observed_models": ["test"], "report": report}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        source = self.root / "source"
        source.mkdir()
        execute(["git", "init", "-b", "main", str(source)])
        (source / "README.md").write_text("Fixture\n")
        git(source, "add", ".")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Initial")
        self.remote = self.root / "remote.git"
        execute(["git", "clone", "--bare", str(source), str(self.remote)])
        self.store = Store(self.root / "state")
        self.project = self.store.register("demo", "example/demo", "main", ["test -f feature.txt"])
        self.github = FakeGitHub(self.remote)
        self.agents = FakeAgents()
        self.team = Coordinator(self.store, self.github, self.agents)

        def local_execute(args, **kwargs):
            if args[:3] == ["gh", "repo", "clone"]:
                return execute(["git", "clone", "--branch", args[-1], str(self.remote), args[4]], **kwargs)
            return execute(args, **kwargs)

        def local_git(cwd, *args):
            args = list(args)
            if "push" in args:
                index = args.index("push")
                args[index + 1] = str(self.remote)
            return git(cwd, *args)

        self.exec_patch = patch("agent_team.coordinator.execute", side_effect=local_execute)
        self.git_patch = patch("agent_team.coordinator.git", side_effect=local_git)
        self.exec_patch.start()
        self.git_patch.start()

    def tearDown(self):
        self.exec_patch.stop()
        self.git_patch.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def tick(self, times=1):
        result = None
        for _ in range(times):
            result = self.team.tick("demo")
        return result

    def test_full_issue_to_ready_with_real_git_and_validation(self):
        run = self.tick(6)
        self.assertEqual(run["stage"], "ready")
        self.assertEqual(run["sha"], run["reviewed_sha"])
        self.assertEqual(self.agents.calls, [("codex", "implement"), ("claude", "review")])
        self.assertEqual(self.github.creates, 1)
        self.assertFalse(self.github.pull["draft"])
        self.assertEqual(self.github.statuses[-1], (run["sha"], "success"))
        self.assertNotIn("merge", [role for _, role in self.agents.calls])
        self.assertEqual(self.tick()["stage"], "idle")
        self.assertEqual(len(self.store.runs()), 1)

    def test_author_rotation_across_projects(self):
        first = self.tick()
        other = self.store.register("other", "example/other", "main", ["true"])
        second = self.store.create(other, {"number": 3, "title": "Second"})
        self.assertEqual(first["author"], "codex")
        self.assertEqual(second["author"], "claude")

    def test_revision_uses_same_author_and_new_review_commit(self):
        self.agents.reject = True
        run = self.tick(5)
        self.assertEqual(run["stage"], "implement")
        old_sha = run["sha"]
        run = self.tick(5)
        self.assertEqual(run["stage"], "ready")
        self.assertNotEqual(old_sha, run["sha"])
        self.assertEqual(run["round"], 1)
        self.assertEqual(self.github.creates, 1)

    def test_revision_limit_blocks(self):
        self.project["max_revisions"] = 0
        self.store.save_project(self.project)
        self.agents.reject = True
        run = self.tick(5)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Revision limit", run["error"])

    def test_validation_failure_never_publishes(self):
        self.project.update(tests=["exit 1"], max_revisions=0)
        self.store.save_project(self.project)
        run = self.tick(3)
        self.assertEqual(run["stage"], "blocked")
        self.assertEqual(self.github.creates, 0)

    def test_edit_after_validation_never_publishes(self):
        run = self.tick(3)
        (self.store.workspace(run) / "feature.txt").write_text("unvalidated change")
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("changed after validation", run["error"])
        self.assertEqual(self.github.creates, 0)

    def test_mutating_validation_is_rejected(self):
        self.project["tests"] = ["echo unexpected >> feature.txt"]
        self.store.save_project(self.project)
        run = self.tick(3)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Validation changed", run["error"])

    def test_quota_wait_no_api_fallback_and_no_busy_retry(self):
        self.tick()
        self.agents.quota = True
        run = self.tick()
        self.assertEqual(run["stage"], "quota_wait")
        calls = len(self.agents.calls)
        self.assertEqual(self.tick()["stage"], "waiting")
        self.assertEqual(len(self.agents.calls), calls)
        self.store.save(run, retry_at=time.time() - 1)
        self.assertEqual(self.tick()["stage"], "validate")

    def test_interruption_requires_explicit_resume(self):
        run = self.tick()
        self.store.save(run, in_flight=True)
        self.assertEqual(self.tick()["stage"], "waiting")
        persisted = self.store.get(run["id"])
        self.assertEqual(persisted["stage"], "blocked")
        self.assertEqual(persisted["resume_stage"], "implement")
        self.assertEqual(self.agents.calls, [])

    def test_pause_does_no_work(self):
        self.project["paused"] = True
        self.store.save_project(self.project)
        self.assertEqual(self.tick()["stage"], "paused")
        self.assertEqual(self.store.runs(), [])

    def test_removed_ready_label_blocks(self):
        self.tick()
        self.github.items[0]["labels"] = []
        self.assertEqual(self.tick()["stage"], "blocked")
        self.assertEqual(self.agents.calls, [])

    def test_head_change_invalidates_ready(self):
        run = self.tick(6)
        self.github.external_sha = "a" * 40
        self.tick()
        self.assertEqual(self.store.get(run["id"])["stage"], "blocked")
        self.assertEqual(self.github.statuses[-1], ("a" * 40, "pending"))

    def test_merged_pr_is_observed_not_reopened(self):
        run = self.tick(6)
        self.github.pull.update(state="closed", merged=True)
        self.tick()
        self.assertEqual(self.store.get(run["id"])["stage"], "merged")

    def test_ci_pending_and_failure_never_ready(self):
        run = self.tick(5)
        self.github.check_state = "pending"
        self.assertEqual(self.tick()["stage"], "ci")
        self.github.check_state = "failure"
        self.assertEqual(self.tick()["stage"], "blocked")
        self.assertTrue(self.github.pull["draft"])

    def test_same_family_review_rejected(self):
        run = self.tick(4)
        self.store.save(run, reviewer=run["author"])
        self.assertEqual(self.tick()["stage"], "blocked")

    def test_state_reopens_after_move(self):
        run = self.tick()
        self.store.db.close()
        moved = self.root / "moved-state"
        (self.root / "state").rename(moved)
        self.store = Store(moved)
        self.team.store = self.store
        self.assertEqual(self.store.get(run["id"])["stage"], "implement")
        self.assertTrue(self.store.workspace(run).exists())
        self.assertEqual(self.tick()["stage"], "validate")

    def test_refresh_retains_old_checkout_and_revalidates(self):
        run = self.tick(6)
        refreshed = self.team.refresh(run["id"])
        self.assertEqual(refreshed["stage"], "validate")
        self.assertIsNone(refreshed["reviewed_sha"])
        self.assertTrue(list(self.store.workspace(run).parent.glob("author-preserved-*")))
        self.assertEqual(self.tick(4)["stage"], "ready")


class ContractTests(unittest.TestCase):
    def test_environment_never_forwards_api_or_github_tokens(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret",
                                     "GH_TOKEN": "secret", "CLAUDE_CODE_USE_BEDROCK": "1"}):
            env = worker_env()
            for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"):
                self.assertNotIn(key, env)
            self.assertIn("HOME", env)

    def test_api_login_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, "", "Logged in using an API key")
        with patch("agent_team.agents.execute", return_value=result):
            with self.assertRaises(TeamError):
                subscription_status("codex")

    def test_claude_api_login_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"loggedIn": True, "authMethod": "api_key"}), "")
        with patch("agent_team.agents.execute", return_value=result):
            with self.assertRaises(TeamError):
                subscription_status("claude")

    def test_malformed_review_fails_closed(self):
        for value in ({"verdict": "pass"}, {"verdict": "maybe", "summary": "", "findings": []}, "pass"):
            with self.assertRaises(TeamError):
                validate_report(value, REVIEW_SCHEMA)

    def test_lock_excludes_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            other = Store(tmp)
            with store.lock():
                with self.assertRaises(TeamError):
                    with other.lock():
                        pass
            store.db.close()
            other.db.close()

    def test_process_timeout_is_bounded(self):
        with self.assertRaises(TeamError):
            execute(["/bin/sh", "-c", "sleep 10"], timeout=0.1)

    def test_duplicate_registration_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.register("one", "example/repo", "main", ["true"])
            with self.assertRaises(TeamError):
                store.register("two", "example/repo", "main", ["true"])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
