import json
import os
import sqlite3
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_team.agents import Agents, FAMILIES, REVIEW_SCHEMA, subscription_status, validate_report
from agent_team.coordinator import Coordinator, GUIDANCE
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
        self.created = []

    def issues(self, project, ready=True):
        return self.items

    def authorized(self, project, issue):
        return issue.get("approved", True)

    def issue(self, repo, number):
        return next(i for i in self.items if i["number"] == number)

    def comment(self, repo, number, marker, body, heading=None):
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

    def setup(self, project):
        pass

    def create_issue(self, project, title, body):
        self.created.append((title, body))
        return {"html_url": f"https://github.com/{project['repo']}/issues/{len(self.created) + 1}"}


class FakeAgents:
    def __init__(self):
        self.calls = []
        self.prompts = {}
        self.reject = False  # True or a count of consecutive rejections
        self.findings = []  # per-rejection findings; the default finding is used when exhausted
        self.quota = False
        self.summary = "Added feature"

    def run(self, agent, role, prompt, cwd, artifacts, project):
        self.calls.append((agent, role))
        self.prompts[role] = prompt
        if self.quota:
            self.quota = False
            raise QuotaError("quota exhausted")
        if role == "implement":
            path = Path(cwd) / "feature.txt"
            path.write_text(path.read_text() + "fixed\n" if path.exists() else "feature\n")
            report = {"summary": self.summary, "limitations": "None"}
        elif role == "discover":
            report = {"issues": [{"title": "Found gap", "evidence": self.summary, "acceptance": "Gap closed"}]}
        elif role == "status":
            # Deterministic stand-in for styled wording: echo the configured status instructions.
            style = prompt.split("Writing standard for each status comment", 1)[1].split("Never omit", 1)[0]
            report = {"message": "Drafted: " + " ".join(style.splitlines()[1:])}
        else:
            default = [{"severity": "high", "location": "feature.txt:1", "evidence": "Bug", "request": "Fix"}]
            findings = (self.findings.pop(0) if self.findings else default) if self.reject else []
            report = {"verdict": "changes_requested" if self.reject else "pass", "summary": self.summary,
                      "findings": findings}
            self.reject = max(int(self.reject) - 1, 0)
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

        def local_clone(repo, destination, base, timeout):
            return execute(["git", "clone", "--branch", base, str(self.remote), str(destination)], timeout=timeout)

        def local_git(cwd, *args):
            args = list(args)
            if "push" in args:
                index = args.index("push")
                args[index + 1] = str(self.remote)
            if "fetch" in args:
                args = [str(self.remote) if str(a).startswith("https://github.com/") else a for a in args]
            return git(cwd, *args)

        self.exec_patch = patch("agent_team.coordinator.clone_repository", side_effect=local_clone)
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

    def existing_feature(self):
        source = self.root / "source"
        (source / "feature.txt").write_text("external feature\n")
        git(source, "add", ".")
        git(source, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "External work")
        git(source, "push", str(self.remote), "HEAD:refs/heads/external")
        return git(source, "rev-parse", "HEAD")

    def selected_ticks(self, run, count):
        for _ in range(count):
            run = self.team.tick("demo", run_id=run["id"])
        return run

    def test_scoped_implementation_has_no_issue_or_github_writes(self):
        run = self.team.select("demo", ["implement"], ["edit"], task="Add feature.txt; preserve existing files")
        self.assertIsNone(run["issue"])
        self.assertEqual(self.agents.calls, [])
        run = self.selected_ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertNotIn("tests", run)
        self.assertEqual(self.github.comments, {})
        self.assertEqual(self.github.statuses, [])
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(run["performed_operations"], ["prepare", "implement"])
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        self.assertEqual(self.selected_ticks(run, 3)["stage"], "stopped")
        self.assertEqual(len(self.agents.calls), 1)

    def test_existing_commit_validation_only_preserves_exact_input(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate"], [], task="Validate existing feature", ref=sha,
                               contributors=["human"])
        run = self.selected_ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["input_revision"], sha)
        self.assertEqual(run["validated_sha"], sha)
        self.assertEqual(self.agents.calls, [])
        self.assertEqual(self.github.comments, {})
        self.assertEqual(self.github.statuses, [])
        self.assertEqual(self.github.creates, 0)
        self.assertIn("implement", run["omitted_operations"])

    def test_existing_branch_publication_then_review_then_readiness(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate"], [], task="Publish existing feature", ref="external",
                               contributors=["human"])
        run = self.selected_ticks(run, 2)
        with self.assertRaises(TeamError):
            self.team.select("demo", ["publish"], [], run_id=run["id"])
        self.assertEqual(self.store.get(run["id"])["grants"], [])
        run = self.team.select("demo", ["publish"], ["push", "github"], run_id=run["id"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(run["sha"], sha)
        self.assertEqual(self.agents.calls, [])
        self.assertNotIn("validate", run["omitted_operations"])
        run = self.team.select("demo", ["review"], [], run_id=run["id"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(len(self.agents.calls), 1)
        self.assertTrue(self.github.pull["draft"])
        with self.assertRaises(TeamError):
            self.team.select("demo", ["ci"], [], run_id=run["id"])
        run = self.team.select("demo", ["ci"], ["readiness"], run_id=run["id"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "ready")
        self.assertFalse(self.github.pull["draft"])
        self.assertEqual(self.github.creates, 1)

    def test_local_review_does_not_require_or_create_pr(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate", "review"], [], task="Review existing feature", ref=sha,
                               contributors=["human"])
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["reviewed_sha"], sha)
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(self.github.comments, {})
        self.assertEqual(self.github.statuses, [])
        self.assertNotIn("publish", run["performed_operations"])

    def test_discovery_and_issue_preparation_stop_without_source_edits(self):
        run = self.team.select("demo", ["discovery"], [], task="Investigate onboarding gaps")
        run = self.selected_ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "issue_prepare")
        self.assertEqual(self.github.created, [])
        self.assertFalse((self.store.workspace(run) / "feature.txt").exists())
        run = self.team.select("demo", ["issue_prepare"], [], run_id=run["id"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["prepared_issues"][0]["title"], "Found gap")
        self.assertEqual(self.github.created, [])
        self.assertEqual(len(self.agents.calls), 1)

    def test_independent_entry_refuses_missing_scope_grants_and_prerequisites(self):
        for operations, grants, options in (
                (["implement"], [], {"task": "Missing edit grant"}),
                (["validate"], [], {"task": "Missing revision"}),
                (["publish"], ["push", "github"], {"task": "Missing validation"}),
                (["review"], [], {"task": "Missing validation"}),
                (["checks"], [], {"task": "Missing tracked PR"}),
                (["ci"], ["github", "readiness"], {"task": "Missing PR and review"}),
                (["revision"], ["edit"], {"task": "Missing rejection history"}),
                (["implement"], ["edit"], {})):
            with self.subTest(operations=operations), self.assertRaises(TeamError):
                self.team.select("demo", operations, grants, **options)
        self.assertEqual(self.store.runs(), [])
        self.assertEqual(self.agents.calls, [])
        self.assertEqual(self.github.comments, {})

    def test_selected_scope_cannot_be_recreated_to_reset_history(self):
        run = self.team.select("demo", ["issue_prepare"], [], task="Prepare a durable task")
        self.selected_ticks(run, 2)
        self.store.save(run, stage="closed")
        with self.assertRaises(TeamError):
            self.team.select("demo", ["implement"], ["edit"], task="Prepare a durable task")
        self.assertEqual(len(self.store.runs()), 1)

    def test_selected_revision_then_local_review_preserves_budget(self):
        self.agents.reject = True
        run = self.team.select("demo", ["implement", "validate", "review"], ["edit"], task="Add feature.txt")
        run = self.selected_ticks(run, 4)
        self.assertEqual(run["stage"], "stopped")
        rejected = run["rejected_shas"][:]
        round_number = run["round"]
        run = self.team.select("demo", ["revision", "validate", "review"], [], run_id=run["id"])
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["round"], round_number)
        self.assertEqual(run["rejected_shas"], rejected)
        self.assertNotEqual(run["sha"], rejected[-1])
        self.assertEqual(run["reviewed_sha"], run["sha"])
        self.assertEqual(self.github.creates, 0)

    def test_selected_watch_detects_configuration_drift_before_publication(self):
        run = self.team.select("demo", ["implement", "validate", "publish"], ["edit", "push", "github"],
                               task="Add feature.txt with publication")
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["stage"], "publish")
        self.store.update_project("demo", tests=["true"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertIsNone(run["validated_sha"])
        self.assertEqual(self.github.creates, 0)

    def test_reviewed_local_candidate_can_select_publication_then_readiness(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate", "review"], [],
                               task="Validate and review existing feature before publication",
                               ref=sha, contributors=["human"])
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["reviewed_sha"], sha)
        calls = list(self.agents.calls)
        run = self.team.select("demo", ["publish", "ci"], ["push", "github", "readiness"],
                               run_id=run["id"])
        self.assertIn("push and draft PR creation/update", run["effect_plan"])
        self.assertIn("CI polling and PR readiness", run["effect_plan"])
        self.assertNotIn("subscription independent review; GitHub evidence only with github grant",
                         run["effect_plan"])
        segment = run["continuations"][-1]
        self.assertEqual(segment["grants"], ["github", "push", "readiness"])
        self.assertEqual(segment["effects"], run["effect_plan"])
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "ci")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(self.agents.calls, calls)
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        run = self.selected_ticks(run, 1)
        self.assertEqual(run["stage"], "ready")
        self.assertFalse(self.github.pull["draft"])
        self.assertEqual(self.agents.calls, calls)
        self.assertEqual(run["performed_operations"], ["prepare", "validate", "review", "publish", "ci"])
        self.assertIn("implement", run["omitted_operations"])

    def test_publication_readiness_sequence_refuses_missing_review_before_writes(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate"], [], task="Validate existing candidate",
                               ref=sha, contributors=["human"])
        run = self.selected_ticks(run, 2)
        with self.assertRaisesRegex(TeamError, "compatible passing review"):
            self.team.select("demo", ["publish", "ci"], ["push", "github", "readiness"],
                             run_id=run["id"])
        saved = self.store.get(run["id"])
        self.assertEqual(saved["stage"], "stopped")
        self.assertEqual(saved["grants"], [])
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(self.github.comments, {})
        self.assertEqual(self.github.statuses, [])

    def test_ci_snapshot_does_not_review_write_or_change_readiness(self):
        sha = self.existing_feature()
        run = self.team.select("demo", ["validate", "publish", "checks"], ["push", "github"],
                               task="Publish existing work and inspect CI without review or readiness",
                               ref=sha, contributors=["human"])
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["stage"], "checks")
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        statuses = list(self.github.statuses)
        comments = dict(self.github.comments)
        for state in ("pending", "failure", "success"):
            self.github.check_state = state
            if state != "pending":
                run = self.team.select("demo", ["checks"], [], run_id=run["id"])
            run = self.selected_ticks(run, 1)
            self.assertEqual(run["stage"], "stopped")
            record = run["ci_checks"][-1]
            self.assertEqual(record["state"], state)
            self.assertEqual(record["sha"], sha)
            self.assertEqual(record["base"], run["base_sha"])
            self.assertFalse(record["readiness_changed"])
            self.assertTrue(self.github.pull["draft"])
            self.assertEqual(self.github.statuses, statuses)
            self.assertEqual(self.github.comments, comments)
            self.assertEqual(self.agents.calls, [])
            self.assertEqual(self.selected_ticks(run, 2)["stage"], "stopped")
        self.assertNotIn("reviewed_sha", run)
        self.assertIn("review", run["unperformed_operations"])
        self.assertIn("ci", run["unperformed_operations"])
        with self.assertRaises(TeamError):
            self.team.select("demo", ["ci"], ["readiness"], run_id=run["id"])

    def test_dirty_handoff_requires_contributors_and_preserves_attribution(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        cwd = self.store.workspace(run)
        (cwd / "human.txt").write_text("human work")
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["publish"])
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["validate"])
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["validate"], [FAMILIES[run["reviewer"]]])
        with self.assertRaises(TeamError):  # records declaration and invalidates old context
            self.team.continue_run(run["id"], ["validate"], ["human"])
        run = self.team.continue_run(run["id"], ["validate"])
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertIn("human", run["contributors"])
        message = git(cwd, "log", "-1", "--format=%B")
        self.assertIn("Contributor: human", message)
        self.assertNotIn("Agent-Family:", message)
        self.assertEqual(self.github.creates, 0)

    def test_ci_stop_point_rejection_stops_before_revision(self):
        self.agents.reject = True
        self.team.tick("demo", 1, "ci")
        for _ in range(4):
            run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "implement")
        self.assertNotIn("ci", run["performed_operations"])
        self.team.tick("demo", 1)
        self.assertEqual(len(self.agents.calls), 2)
        self.assertTrue(self.github.pull["draft"])

    def test_dirty_same_path_handoff_detects_content_change(self):
        self.team.tick("demo", 1, "implement")
        run = self.team.tick("demo", 1)
        cwd = self.store.workspace(run)
        old_status = git(cwd, "status", "--porcelain")
        (cwd / "feature.txt").write_text("external replacement\n")
        self.assertEqual(git(cwd, "status", "--porcelain"), old_status)
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["validate"])
        self.assertTrue(self.store.get(run["id"])["pending_contribution"])
        self.assertEqual(len(self.agents.calls), 1)

    def test_issue_selection_keeps_approval_but_omits_unauthorized_writes(self):
        self.github.items[0]["approved"] = False
        with self.assertRaises(TeamError):
            self.team.select("demo", ["implement"], ["edit"], issue_number=1)
        self.assertEqual(self.store.runs(), [])
        self.github.items[0]["approved"] = True
        run = self.team.select("demo", ["implement"], ["edit"], issue_number=1)
        run = self.selected_ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(self.github.comments, {})
        self.github.items[0]["body"] = "Different scope"
        with self.assertRaises(TeamError):
            self.team.select("demo", ["validate"], [], run_id=run["id"])
        self.assertEqual(self.store.get(run["id"])["stage"], "stopped")

    def test_selected_exhaustion_extension_does_not_expand_old_selection(self):
        self.store.update_project("demo", max_revisions=0, tests=["exit 1"])
        run = self.team.select("demo", ["implement", "validate"], ["edit"], task="Add bounded feature.txt")
        run = self.selected_ticks(run, 3)
        self.assertEqual(run["stage"], "handoff")
        with self.assertRaises(TeamError):
            self.team.select("demo", ["revision", "validate"], [], run_id=run["id"])
        run = self.team.decide(run["id"], "extend", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "revision")
        self.selected_ticks(run, 2)
        self.assertEqual(len(self.agents.calls), 1)
        self.assertEqual(run["revision_limit"], 1)
        self.assertEqual(self.github.comments, {})

    def test_unpublished_refresh_swap_reconciles_after_restart(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        self.store.save(run, stage="stale")
        with patch.object(self.team, "finish_local_integration", side_effect=TeamError("Interrupted before swap")):
            with self.assertRaises(TeamError):
                self.team.refresh(run["id"])
        saved = self.store.get(run["id"])
        pending = saved["pending_integration"]
        # Simulate interruption after the original checkout was preserved.
        self.store.workspace(saved).rename(Path(pending["preserved"]))
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        run = self.team.refresh(saved["id"])
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "validate")
        self.assertIsNone(run["pending_integration"])
        self.assertTrue(Path(pending["preserved"]).exists())
        self.assertEqual(self.github.creates, 0)

    def test_legacy_registry_migration_keeps_issue_history_and_allows_tasks(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        record = self.store.create(self.project, self.github.items[0])
        record.update(revision_limit=1, rejected_shas=["a" * 40], round=1)
        db = sqlite3.connect(legacy / "state.sqlite3")
        db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, project TEXT NOT NULL, "
                   "issue INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(project, issue))")
        db.execute("INSERT INTO runs VALUES (?, ?, ?, ?)",
                   (record["id"], "demo", 1, json.dumps(record)))
        db.commit()
        db.close()
        store = Store(legacy)
        try:
            self.assertEqual(store.get(record["id"]), record)
            project = store.register("demo", "example/demo", "main", ["true"])
            for scope in ("First task", "Second task"):
                task = store.create(project, {"number": None, "title": scope, "body": scope})
                self.assertIsNone(task["issue"])
            self.assertEqual(len(store.runs()), 3)
        finally:
            store.db.close()

    def test_declared_human_repair_can_validate_and_review_without_author_pass(self):
        self.agents.reject = True
        run = self.team.select("demo", ["implement", "validate", "review"], ["edit"], task="Add human-repairable feature")
        run = self.selected_ticks(run, 4)
        self.assertEqual(run["stage"], "stopped")
        round_number = run["round"]
        rejected = run["rejected_shas"][:]
        (self.store.workspace(run) / "feature.txt").write_text("human repair\n")
        with self.assertRaises(TeamError):
            self.team.select("demo", ["validate", "review"], [], run_id=run["id"], contributors=["human"])
        run = self.team.select("demo", ["validate", "review"], [], run_id=run["id"])
        run = self.selected_ticks(run, 2)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["round"], round_number)
        self.assertEqual(run["rejected_shas"], rejected)
        self.assertNotEqual(run["sha"], rejected[-1])
        self.assertEqual([role for _, role in self.agents.calls], ["implement", "review", "review"])
        self.assertEqual(run["reviewed_sha"], run["sha"])
        self.assertEqual(self.github.creates, 0)

    def test_explicit_continuation_preserves_evidence_and_stops_again(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        sha = run["validated_sha"]
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        resumed = self.team.continue_run(run["id"], ["publish"])
        self.assertEqual(resumed["validated_sha"], sha)
        self.team.continue_run(run["id"], ["publish"])
        self.assertEqual(len(self.store.get(run["id"])["continuations"]), 1)
        result = self.team.tick("demo", 1)
        self.assertEqual(result["stage"], "stopped")
        self.assertEqual(result["next_stage"], "review")
        self.assertEqual(len(self.agents.calls), 1)
        self.assertEqual(self.github.creates, 1)

    def test_continuation_invalidates_configuration_evidence(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        self.store.update_project("demo", tests=["true"])
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["publish"])
        saved = self.store.get(run["id"])
        self.assertIsNone(saved["validated_sha"])
        self.assertEqual(saved["next_stage"], "validate")
        self.assertEqual(len(saved["evidence_invalidations"]), 1)
        self.assertEqual(self.github.creates, 0)
        self.team.continue_run(run["id"], ["validate"])
        self.assertEqual(self.team.tick("demo", 1)["stage"], "stopped")

    def test_continuation_detects_dirty_dependency_pin(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        (self.store.workspace(run) / "requirements.lock").write_text("changed pin")
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["publish"])
        saved = self.store.get(run["id"])
        self.assertIsNone(saved["validated_sha"])
        self.assertEqual(saved["next_stage"], "validate")
        self.assertEqual(self.github.creates, 0)

    def test_continuation_detects_unpublished_base_drift(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        source = self.root / "source"
        (source / "base.txt").write_text("new base")
        git(source, "add", ".")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Base update")
        git(source, "push", str(self.remote), "main")
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["publish"])
        saved = self.store.get(run["id"])
        self.assertEqual(saved["stage"], "stale")
        self.assertIsNone(saved["validated_sha"])
        self.assertEqual(self.github.creates, 0)
        refreshed = self.team.refresh(run["id"])
        self.assertEqual(refreshed["stage"], "stopped")
        self.assertEqual(refreshed["next_stage"], "validate")
        self.assertIsNone(refreshed["review_record"])
        self.assertEqual(self.github.creates, 0)
        self.team.continue_run(run["id"], ["validate"])
        validated = self.team.tick("demo", 1)
        self.assertEqual(validated["stage"], "stopped")
        self.assertEqual(validated["validated_sha"], validated["sha"])
        self.assertTrue((self.store.workspace(validated) / "base.txt").exists())

    def test_continuation_cannot_reset_exhausted_budget(self):
        self.store.update_project("demo", max_revisions=0, tests=["exit 1"])
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "handoff")
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["implement", "validate"])
        saved = self.store.get(run["id"])
        self.assertEqual(saved["rejected_shas"], run["rejected_shas"])
        self.assertEqual(saved["revision_limit"], 0)

    def test_continuation_refuses_scope_change_and_skipped_prerequisite(self):
        self.team.tick("demo", 1, "implement")
        run = self.team.tick("demo", 1)
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["publish"])
        self.github.items[0]["body"] = "Changed scope"
        with self.assertRaises(TeamError):
            self.team.continue_run(run["id"], ["validate"])
        self.assertEqual(self.store.get(run["id"])["stage"], "stopped")
        self.assertEqual(self.github.creates, 0)

    def test_continuation_keeps_rejected_commit_and_round(self):
        self.agents.reject = True
        self.team.tick("demo", 1, "review")
        for _ in range(4):
            run = self.team.tick("demo", 1)
        rejected = list(run["rejected_shas"])
        round_number = run["round"]
        result = self.team.continue_run(run["id"], ["implement", "validate", "publish", "review"])
        self.assertEqual(result["round"], round_number)
        self.assertEqual(result["rejected_shas"], rejected)
        for _ in range(4):
            result = self.team.tick("demo", 1)
        self.assertEqual(result["stage"], "stopped")
        self.assertNotEqual(result["sha"], rejected[-1])
        self.assertEqual(result["next_stage"], "ci")

    def test_partial_stop_survives_restart_and_untargeted_ticks(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "publish")
        self.assertEqual(run["omitted_operations"], ["publish", "review", "ci"])
        self.assertEqual(self.github.creates, 0)
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        self.assertEqual(self.team.tick("demo", 1)["stage"], "stopped")
        self.assertEqual(self.tick()["stage"], "waiting")
        self.assertEqual(self.github.creates, 0)
        self.assertEqual(len(self.agents.calls), 1)

    def test_partial_boundary_cannot_expand(self):
        self.team.tick("demo", 1, "implement")
        with self.assertRaises(TeamError):
            self.team.tick("demo", 1, "ci")
        with self.assertRaises(TeamError):
            self.team.tick("demo", stop_after="validate")
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertNotIn("tests", run)
        self.assertEqual(self.github.creates, 0)

    def test_partial_validation_failure_does_not_apply_fixes(self):
        self.store.update_project("demo", tests=["exit 1"])
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["round"], 1)
        self.assertIn(run["sha"], run["rejected_shas"])
        self.team.tick("demo", 1)
        self.assertEqual(len(self.agents.calls), 1)
        self.assertEqual(self.github.creates, 0)

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

    def test_partial_ci_pending_then_success(self):
        self.team.tick("demo", 1, "ci")
        for _ in range(4):
            self.team.tick("demo", 1)
        self.github.check_state = "pending"
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "ci")
        self.assertTrue(self.github.pull["draft"])
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        self.github.check_state = "success"
        run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "ready")
        self.github.pull["state"] = "closed"
        self.team.tick("demo", 1)
        self.assertEqual(self.store.get(run["id"])["stage"], "closed")

    def test_partial_crash_window_resume_and_quota_retry(self):
        self.team.tick("demo", 1, "validate")
        self.team.tick("demo", 1)
        run = self.team.tick("demo", 1)
        # Simulate validate's successor save before the endpoint save.
        self.store.save(run, stage="publish", in_flight=True)
        self.team.tick("demo", 1)
        self.assertEqual(self.store.get(run["id"])["stage"], "stopped")
        self.store.save(run, stage="blocked", resume_stage="publish")
        self.assertEqual(self.team.resume(run["id"])["stage"], "stopped")
        self.store.save(run, stage="quota_wait", resume_stage="publish", retry_at=0)
        self.team.tick("demo", 1)
        self.assertEqual(self.store.get(run["id"])["stage"], "stopped")
        self.assertEqual(self.github.creates, 0)

    def test_partial_publish_stops_before_review_and_reconciles_close(self):
        self.team.tick("demo", 1, "publish")
        for _ in range(3):
            run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "review")
        self.assertEqual(self.github.creates, 1)
        self.assertEqual(len(self.agents.calls), 1)
        self.github.pull["state"] = "closed"
        self.team.tick("demo", 1)
        self.assertEqual(self.store.get(run["id"])["stage"], "closed")

    def test_partial_review_rejection_does_not_apply_fixes(self):
        self.agents.reject = True
        self.team.tick("demo", 1, "review")
        for _ in range(4):
            run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "implement")
        self.assertIn(run["sha"], run["rejected_shas"])
        self.team.tick("demo", 1)
        self.assertEqual(len(self.agents.calls), 2)

    def test_full_review_rejection_does_not_add_partial_fields(self):
        self.agents.reject = True
        run = self.tick(5)
        self.assertEqual(run["stage"], "implement")
        self.assertNotIn("next_stage", run)
        self.assertNotIn("partial_result", run)

    def test_partial_review_pass_stops_before_ci_and_detects_changed_head(self):
        self.team.tick("demo", 1, "review")
        for _ in range(4):
            run = self.team.tick("demo", 1)
        self.assertEqual(run["stage"], "stopped")
        self.assertEqual(run["next_stage"], "ci")
        self.assertTrue(self.github.pull["draft"])
        self.github.external_sha = "f" * 40
        self.team.tick("demo", 1)
        self.assertEqual(self.store.get(run["id"])["stage"], "stale")

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

    def test_revision_limit_hands_off(self):
        self.project["max_revisions"] = 0
        self.store.save_project(self.project)
        self.agents.reject = True
        run = self.tick(5)
        self.assertEqual(run["stage"], "handoff")
        self.assertIn("Revision limit", run["error"])

    def exhaust(self):
        """Two rejected reviews with max_revisions=1 reach the handoff on the existing PR."""
        self.project["max_revisions"] = 1
        self.store.save_project(self.project)
        self.agents.reject = 2
        self.agents.findings = [
            [{"severity": "high", "location": "feature.txt:1", "evidence": "Bug", "request": "Fix"}],
            [{"severity": "high", "location": "feature.txt:1", "evidence": "Still", "request": " fix"},
             {"severity": "low", "location": "feature.txt:4", "evidence": "Style", "request": "Rename"},
             {"severity": "medium", "location": "other.py:2", "evidence": "Regression", "request": "Test"}]]
        run = self.tick(9)
        self.assertEqual(run["stage"], "handoff")
        return run

    def push_repair(self, run, message="Human repair", text="repaired\n"):
        work = self.root / f"repair-{time.time_ns()}"
        execute(["git", "clone", "--branch", run["branch"], str(self.remote), str(work)])
        (work / "feature.txt").write_text(text)
        git(work, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-am", message)
        git(work, "push", "origin", "HEAD")
        return git(work, "rev-parse", "HEAD")

    def command(self, *args):
        from agent_team.cli import dispatch, parser
        with patch("agent_team.cli.Coordinator", return_value=self.team), patch("agent_team.cli.GitHub"), \
                patch("agent_team.cli.emit"):
            return dispatch(parser().parse_args(list(args)), self.store)

    def test_exhaustion_persists_evidence_and_publishes_handoff(self):
        run = self.exhaust()
        calls = len(self.agents.calls)
        history = run["revision_history"]
        self.assertEqual([e["round"] for e in history], [0, 1])
        self.assertEqual(run["rejected_shas"], [history[0]["sha"], run["sha"]])
        self.assertEqual(run["review_record"]["report"]["verdict"], "changes_requested")
        self.assertIn("Regression", run["feedback"])
        self.assertEqual([f["match"] for f in history[0]["findings"]], ["first"])
        self.assertEqual([f["match"] for f in history[1]["findings"]], ["repeated", "uncertain", "new"])
        body = self.github.comments[(7, f"{run['id']}-handoff-1")]
        for text in (run["id"], "issue #1", "PR #7", f"Candidate commit `{run['sha']}`", "`test -f feature.txt` exit 0",
                     "Evidence: Regression", "repeated: an earlier round", "uncertain: an earlier round",
                     "new: no earlier finding", "Revision 0: review by `claude` (anthropic)", "Revision 1: review",
                     "https://github.com/example/demo/pull/7", f"https://github.com/example/demo/commit/{run['sha']}",
                     f"agent-team decide {run['id']} extend", "Only the maintainer decides"):
            self.assertIn(text, body)
        self.assertEqual(self.github.statuses[-1], (run["sha"], "failure"))
        self.assertEqual(run["outbox"], [])
        self.assertIn("operator decision", self.github.comments[(1, run["id"])])
        # No silent retry, no resume, no refresh past the evaluation point, and no new intake.
        self.github.items.append(dict(self.github.items[0], number=2, title="Next"))
        self.assertEqual(self.tick(3)["stage"], "waiting")
        self.assertEqual(len(self.agents.calls), calls)
        with self.assertRaises(TeamError):
            self.command("resume", run["id"])
        with self.assertRaises(TeamError):
            self.team.refresh(run["id"])
        self.assertEqual(len(self.store.runs()), 1)

    def test_finite_extension_keeps_history_and_requires_new_evidence(self):
        run = self.exhaust()
        for bad in ((None,), (0,), (4,)):
            with self.assertRaises(TeamError):
                self.team.decide(run["id"], "extend", *bad)
        with self.assertRaises(TeamError):
            self.team.decide(run["id"], "stop", 1)
        self.command("decide", run["id"], "extend", "--revisions", "1", "--note", "One more try")
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["round"], run["extension"]), ("implement", 2, 1))
        self.assertEqual(run["decisions"][0]["action"], "extend")
        self.assertEqual(len(run["revision_history"]), 2)
        self.assertIn("Regression", run["feedback"])
        decision = self.github.comments[(7, f"{run['id']}-decision-1")]
        self.assertIn("1 more revision(s); the limit is now 2", decision)
        self.assertIn("Operator note: One more try", decision)
        with self.assertRaises(TeamError):
            self.team.decide(run["id"], "extend", 1)  # decisions apply only at a handoff
        # Using up the extension hands off again instead of retrying.
        self.agents.reject = True
        run = self.tick(4)
        self.assertEqual((run["stage"], len(run["revision_history"])), ("handoff", 3))
        self.assertIn("Operator decision after revision 1: extend (1 more)",
                      self.github.comments[(7, f"{run['id']}-handoff-2")])
        self.team.decide(run["id"], "extend", 1)
        self.assertEqual(self.tick(5)["stage"], "ready")

    def test_config_change_before_extension_cannot_widen_it(self):
        run = self.exhaust()
        self.assertEqual(run["revision_limit"], 1)
        self.project["max_revisions"] = 100
        self.store.save_project(self.project)
        self.team.decide(run["id"], "extend", 1)
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["round"], run["revision_limit"], run["extension"]),
                         ("implement", 2, 2, 1))
        self.assertEqual(run["decisions"][0]["limit"], 2)
        self.assertIn("the limit is now 2", self.github.comments[(7, f"{run['id']}-decision-1")])
        # The one authorized revision is used up: a rejection hands off again.
        self.agents.reject = True
        run = self.tick(4)
        self.assertEqual((run["stage"], run["round"], len(run["revision_history"])), ("handoff", 2, 3))

    def test_config_change_after_extension_cannot_widen_it(self):
        run = self.exhaust()
        self.team.decide(run["id"], "extend", 1)
        self.project["max_revisions"] = 100
        self.store.save_project(self.project)
        self.agents.reject = True
        run = self.tick(4)
        self.assertEqual((run["stage"], run["round"], run["revision_limit"]), ("handoff", 2, 2))
        self.assertIn("revision 2/2", self.github.comments[(7, f"{run['id']}-handoff-2")])
        # Lowering the configuration cannot shrink an authorized extension either.
        self.team.decide(run["id"], "extend", 1)
        self.project["max_revisions"] = 0
        self.store.save_project(self.project)
        run = self.store.get(run["id"])
        self.assertEqual((run["round"], run["revision_limit"], run["extension"]), (3, 3, 2))
        self.assertEqual(self.tick(5)["stage"], "ready")

    def test_no_change_extension_cannot_reroll_rejected_evidence(self):
        run = self.exhaust()
        self.team.decide(run["id"], "extend", 1)
        with patch.object(self.agents, "run", return_value={"report": {"summary": "", "limitations": ""}}):
            run = self.tick()
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("no new commit", run["error"])

    def test_stop_and_rescope_close_locally_and_keep_github_open(self):
        for action in ("stop", "rescope"):
            with self.subTest(action=action):
                self.tearDown()
                self.setUp()
                run = self.exhaust()
                self.team.decide(run["id"], action)
                run = self.store.get(run["id"])
                self.assertEqual(run["stage"], "closed")
                self.assertEqual(self.github.pull["state"], "open")
                self.assertIn(f"Agent Team decision: {action}", self.github.comments[(7, f"{run['id']}-decision-1")])
                self.assertEqual(len(run["revision_history"]), 2)

    def test_direct_repair_is_adopted_revalidated_and_independently_reviewed(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        self.assertEqual(self.store.get(run["id"])["stage"], "repair")
        self.assertEqual(self.tick()["stage"], "waiting")
        with self.assertRaises(TeamError):  # nothing new on the branch yet
            self.team.adopt(run["id"], ["human"])
        head = self.push_repair(run)
        with self.assertRaises(TeamError):  # the reviewer's family cannot review its own repair
            self.team.adopt(run["id"], ["anthropic"])
        with self.assertRaises(TeamError):
            self.team.decide(run["id"], "extend", 1)
        self.command("adopt", run["id"], "--contributor", "human")
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"], run["round"]), ("validate", head, 2))
        self.assertEqual(run["adoptions"][0]["families"], ["openai"])
        self.assertIn("human", run["contributors"])
        self.assertIn((head, "pending"), self.github.statuses)
        self.assertIn("Declared contributors: human", self.github.comments[(7, f"{run['id']}-adopt-2")])
        calls = len(self.agents.calls)
        run = self.tick(4)
        self.assertEqual(run["stage"], "ready")
        self.assertEqual(run["reviewed_sha"], head)
        self.assertEqual(self.agents.calls[calls:], [("claude", "review")])

    def test_validation_exhaustion_after_review_rejection_keeps_unverified_findings(self):
        self.project["max_revisions"] = 1
        self.store.save_project(self.project)
        self.agents.reject = 1
        self.agents.findings = [[{"severity": "high", "location": "feature.txt:1", "evidence": "Bug",
                                  "request": "Fix the bug"}]]
        run = self.tick(5)
        self.assertEqual((run["stage"], run["round"]), ("implement", 1))
        self.project["tests"] = ["test -f feature.txt", "false"]
        self.store.save_project(self.project)
        for _ in range(5):
            run = self.tick()
            if run["stage"] == "handoff":
                break
        self.assertEqual(run["stage"], "handoff")
        self.assertEqual([e["kind"] for e in run["revision_history"]], ["review", "validation"])
        body = self.github.comments[(7, f"{run['id']}-handoff-1")]
        for text in ("Remaining findings from validation (1)", "`false` exit 1",
                     "Earlier review findings with unverified resolution (1)",
                     f"Review of revision 0 rejected `{run['revision_history'][0]['sha']}`",
                     "status uncertain", "Evidence: Bug", "Request: Fix the bug"):
            self.assertIn(text, body)
        self.team.decide(run["id"], "extend", 1)
        feedback = self.store.get(run["id"])["feedback"]
        self.assertIn("Validation failed", feedback)
        self.assertIn("no later review verified", feedback)
        self.assertIn("Fix the bug", feedback)

    def test_handoff_after_review_lists_only_latest_review_findings(self):
        run = self.exhaust()
        self.assertNotIn("unverified resolution", self.github.comments[(7, f"{run['id']}-handoff-1")])

    def test_force_pushed_repair_that_drops_rejected_history_is_refused(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        work2 = self.root / "extend"  # cloned before the rewrite, so it still has the rejected candidate
        execute(["git", "clone", str(self.remote), str(work2)])
        work = self.root / "rewrite"
        execute(["git", "clone", "--branch", "main", str(self.remote), str(work)])
        (work / "feature.txt").write_text("rewritten\n")
        git(work, "add", "feature.txt")
        git(work, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Rewritten repair")
        git(work, "push", "--force", "origin", f"HEAD:{run['branch']}")
        head = git(work, "rev-parse", "HEAD")
        calls = len(self.agents.calls)
        with self.assertRaises(TeamError) as caught:
            self.team.adopt(run["id"], ["human"])
        self.assertIn("without rewriting history", str(caught.exception))
        after = self.store.get(run["id"])
        self.assertEqual((after["stage"], after["sha"], after["published_sha"], after.get("adoptions", [])),
                         ("repair", run["sha"], run["published_sha"], []))
        self.assertEqual(git(self.store.workspace(after), "rev-parse", "HEAD"), run["sha"])
        self.assertNotIn(head, [sha for sha, _ in self.github.statuses])
        self.assertNotIn((7, f"{run['id']}-adopt-2"), self.github.comments)
        self.assertEqual(self.agents.calls[calls:], [])
        # A repair that extends the rejected candidate is still adoptable afterwards.
        git(work2, "checkout", "-B", run["branch"], run["published_sha"])
        (work2 / "feature.txt").write_text("repaired\n")
        git(work2, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-am", "Repair")
        git(work2, "push", "--force", "origin", f"HEAD:{run['branch']}")
        self.team.adopt(run["id"], ["human"])
        self.assertEqual(self.store.get(run["id"])["stage"], "validate")

    def test_repair_trailers_reveal_reviewer_family(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        self.push_repair(run, "Repair\n\nAgent-Family: anthropic")
        with self.assertRaises(TeamError):
            self.team.adopt(run["id"], ["human"])
        self.assertEqual(self.store.get(run["id"])["stage"], "repair")

    def test_head_change_after_adoption_needs_new_adoption_and_independent_review(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        self.push_repair(run)
        self.team.adopt(run["id"], ["human"])
        calls = len(self.agents.calls)
        self.push_repair(run, "Second repair\n\nAgent-Family: anthropic", "repaired again\n")
        self.tick()
        run = self.store.get(run["id"])
        self.assertEqual(run["stage"], "stale")
        with self.assertRaises(TeamError):  # refresh cannot take a new external head without declarations
            self.team.refresh(run["id"])
        with self.assertRaises(TeamError):  # trailers show the reviewer's family contributed
            self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual(run["stage"], "stale")
        self.assertNotIn("anthropic", run["contributors"])
        self.assertEqual(len(run["adoptions"]), 1)
        self.assertEqual(self.agents.calls[calls:], [])

    def test_external_repair_after_extension_needs_declared_contributors(self):
        run = self.exhaust()
        self.team.decide(run["id"], "extend", 1)
        calls = len(self.agents.calls)
        # The reviewer's family repairs the branch without an Agent-Family trailer.
        head = self.push_repair(run, "Repair by the reviewer's family")
        self.assertEqual(self.tick()["stage"], "stale")
        self.assertIn("agent-team adopt", self.store.get(run["id"])["error"])
        with self.assertRaises(TeamError):  # refresh cannot take the head on missing trailers
            self.team.refresh(run["id"])
        with self.assertRaises(TeamError):
            self.team.adopt(run["id"], ["anthropic"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["extension"], len(run["decisions"])), ("stale", 1, 1))
        self.assertNotEqual(run["published_sha"], head)
        self.assertEqual(self.agents.calls[calls:], [])
        self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"], run["round"]), ("validate", head, 3))
        self.assertEqual((run["extension"], len(run["decisions"]), len(run["adoptions"])), (1, 1, 1))
        run = self.tick(4)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("ready", head))
        self.assertEqual(self.agents.calls[calls:], [("claude", "review")])

    def test_second_human_repair_after_adoption_is_adopted_again(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        self.push_repair(run)
        self.team.adopt(run["id"], ["human"])
        head = self.push_repair(run, "Second repair", "repaired again\n")
        self.assertEqual(self.tick()["stage"], "stale")
        self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"], len(run["adoptions"])), ("validate", head, 2))
        calls = len(self.agents.calls)
        run = self.tick(4)
        self.assertEqual((run["stage"], run["reviewed_sha"]), ("ready", head))
        self.assertEqual(self.agents.calls[calls:], [("claude", "review")])

    def test_extension_after_rejected_adoption_counts_from_handoff_round(self):
        run = self.exhaust()
        self.team.decide(run["id"], "repair")
        self.push_repair(run)
        self.team.adopt(run["id"], ["human"])
        self.agents.reject = True
        run = self.tick(3)
        self.assertEqual((run["stage"], run["round"]), ("handoff", 2))  # adoption passed the limit of 1
        self.team.decide(run["id"], "extend", 2)
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["round"], run["extension"]), ("implement", 3, 3))
        self.assertEqual([(d["action"], d["revisions"], d["limit"]) for d in run["decisions"]],
                         [("repair", None, 1), ("extend", 2, 4)])
        self.assertIn("2 more revision(s); the limit is now 4", self.github.comments[(7, f"{run['id']}-decision-2")])
        # Both authorized revisions are usable: a rejection at round 3 revises instead of handing off.
        self.agents.reject = True
        run = self.tick(4)
        self.assertEqual((run["stage"], run["round"]), ("implement", 4))
        self.assertEqual(len(run["revision_history"]), 4)
        self.assertEqual(self.tick(5)["stage"], "ready")

    def test_stale_recovered_run_unadoptable_by_reviewer_family_can_stop_or_rescope(self):
        for action in ("stop", "rescope"):
            with self.subTest(action=action):
                self.tearDown()
                self.setUp()
                run = self.exhaust()
                self.team.decide(run["id"], "extend", 1)
                self.push_repair(run, "Repair by the reviewer's family")
                self.assertEqual(self.tick()["stage"], "stale")
                calls = len(self.agents.calls)
                with self.assertRaises(TeamError):
                    self.team.adopt(run["id"], ["anthropic"])
                for refused in (("extend", 1), ("repair",)):
                    with self.assertRaises(TeamError):
                        self.team.decide(run["id"], *refused)
                real = self.github.comment
                def fail_decision(repo, number, marker, *args, **kwargs):
                    if "-decision-" in marker:
                        raise TeamError("GitHub unavailable")
                    return real(repo, number, marker, *args, **kwargs)
                with patch.object(self.github, "comment", side_effect=fail_decision):
                    with self.assertRaises(TeamError):
                        self.command("decide", run["id"], action, "--note", "Reviewer family repaired it")
                # The decision is durable and its comment stays queued even though publication failed.
                run = self.store.get(run["id"])
                self.assertEqual(run["stage"], "closed")
                self.assertEqual([d["action"] for d in run["decisions"]], ["extend", action])
                self.assertEqual(len(run["revision_history"]), 2)
                self.assertEqual([w["marker"] for w in run["outbox"]], [f"{run['id']}-decision-2"])
                self.tick()
                run = self.store.get(run["id"])
                self.assertEqual(run["outbox"], [])
                body = self.github.comments[(7, f"{run['id']}-decision-2")]
                self.assertIn(f"Agent Team decision: {action}", body)
                self.assertIn("Operator note: Reviewer family repaired it", body)
                self.assertEqual(self.github.pull["state"], "open")
                self.assertEqual(self.agents.calls[calls:], [])

    def test_interruption_right_after_handoff_save_keeps_decisions(self):
        real = self.team.revise
        def crash(project, run, *args, **kwargs):
            real(project, run, *args, **kwargs)
            if run["stage"] == "handoff":
                raise KeyboardInterrupt()
        with patch.object(self.team, "revise", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.exhaust()
        run = self.store.runs()[0]
        # Review comment, review status, handoff comment, handoff status.
        self.assertEqual((run["stage"], run["in_flight"], len(run["outbox"])), ("handoff", False, 4))
        # State left by an older coordinator that saved the handoff while still in flight.
        self.store.save(run, in_flight=True)
        calls = len(self.agents.calls)
        self.assertEqual(self.tick()["stage"], "waiting")
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["in_flight"], run["outbox"]), ("handoff", False, []))
        self.assertIn((7, f"{run['id']}-handoff-1"), self.github.comments)
        self.assertEqual(len(self.agents.calls), calls)
        with self.assertRaises(TeamError):
            self.team.refresh(run["id"])
        self.team.decide(run["id"], "repair")
        self.assertEqual(self.store.get(run["id"])["stage"], "repair")

    def test_refresh_refuses_blocked_handoff(self):
        run = self.exhaust()
        self.store.save(run, stage="blocked", resume_stage="handoff")
        with self.assertRaises(TeamError):
            self.team.refresh(run["id"])

    def test_interrupted_handoff_publication_is_retried_without_agents(self):
        real = self.github.comment
        def fail_handoff(repo, number, marker, *args, **kwargs):
            if "-handoff-" in marker:
                raise TeamError("GitHub unavailable")
            return real(repo, number, marker, *args, **kwargs)
        with patch.object(self.github, "comment", side_effect=fail_handoff):
            with self.assertRaises(TeamError):
                self.exhaust()
        run = self.store.runs()[0]
        self.assertEqual(run["stage"], "handoff")
        self.assertEqual(len(run["outbox"]), 2)
        calls = len(self.agents.calls)
        self.tick()
        run = self.store.get(run["id"])
        self.assertEqual(run["outbox"], [])
        self.assertIn((7, f"{run['id']}-handoff-1"), self.github.comments)
        self.assertEqual(len(self.agents.calls), calls)

    def test_failed_review_writes_at_exhaustion_still_record_handoff(self):
        for failing in ("comment", "status"):
            with self.subTest(failing=failing):
                self.tearDown()
                self.setUp()
                real = getattr(self.github, failing)
                seen = []
                def fail(repo, target, *args, **kwargs):
                    rejected = ("-review-1-" in args[0]) if failing == "comment" else (
                        args[-1] == "Independent reviewer requested changes")
                    if rejected:
                        seen.append(target)
                        if failing == "comment" or len(seen) > 1:  # the round-0 status succeeds
                            raise TeamError("GitHub unavailable")
                    return real(repo, target, *args, **kwargs)
                with patch.object(self.github, failing, side_effect=fail):
                    with self.assertRaises(TeamError):
                        self.exhaust()
                    run = self.store.runs()[0]
                    # Recorded before any GitHub write: no resume needed, decisions available.
                    self.assertEqual((run["stage"], run["in_flight"]), ("handoff", False))
                    self.assertEqual([e["round"] for e in run["revision_history"]], [0, 1])
                    self.assertIn("Regression", run["feedback"])
                    self.assertIn(run["sha"], run["rejected_shas"])
                    self.assertTrue(run["outbox"])
                    self.assertEqual(len(run["handoffs"]), 1)
                    with self.assertRaises(TeamError):  # decision is saved; its publication waits
                        self.team.decide(run["id"], "repair")
                run = self.store.get(run["id"])
                self.assertEqual((run["stage"], run["decisions"][0]["action"]), ("repair", "repair"))
                calls = len(self.agents.calls)
                self.tick()
                run = self.store.get(run["id"])
                self.assertEqual(run["outbox"], [])
                self.assertIn((7, f"{run['id']}-review-1-{run['sha']}"), self.github.comments)
                self.assertIn((7, f"{run['id']}-handoff-1"), self.github.comments)
                self.assertIn((7, f"{run['id']}-decision-1"), self.github.comments)
                self.assertIn((run["sha"], "failure"), self.github.statuses)
                self.assertEqual(len(self.agents.calls), calls)

    def test_interrupted_review_before_handoff_reuses_verdict(self):
        self.project["max_revisions"] = 0
        self.store.save_project(self.project)
        self.agents.reject = True
        self.tick(4)
        with patch.object(self.team, "revise", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.assertEqual(self.tick()["stage"], "waiting")
        run = self.store.runs()[0]
        self.assertEqual((run["stage"], run["resume_stage"]), ("blocked", "review"))
        self.command("resume", run["id"])
        calls = len(self.agents.calls)
        run = self.tick()
        self.assertEqual(run["stage"], "handoff")
        self.assertEqual(len(self.agents.calls), calls)  # the persisted verdict is not rerolled

    def test_refresh_after_interrupted_rejection_records_it_instead_of_rerolling(self):
        for limit, stage, round_ in ((0, "handoff", 0), (1, "implement", 1)):
            with self.subTest(limit=limit):
                self.tearDown()
                self.setUp()
                self.project["max_revisions"] = limit
                self.store.save_project(self.project)
                self.agents.reject = True
                self.tick(4)
                with patch.object(self.team, "revise", side_effect=KeyboardInterrupt()):
                    with self.assertRaises(KeyboardInterrupt):
                        self.tick()
                self.tick()
                run = self.store.runs()[0]
                pull = self.github.pr(None, 7)
                sha, head, base = run["sha"], pull["head"]["sha"], pull["base"]["sha"]
                self.assertEqual((run["stage"], run["resume_stage"], run["review_sha"]), ("blocked", "review", sha))
                self.assertNotIn(sha, run.get("rejected_shas", []))
                calls = len(self.agents.calls)
                # Head and base are unchanged; refresh must not discard the verdict and review the same commit.
                self.team.refresh(run["id"])
                run = self.store.get(run["id"])
                self.assertEqual((run["stage"], run["round"], run["sha"]), (stage, round_, sha))
                self.assertIn(sha, run["rejected_shas"])
                self.assertEqual([(e["round"], e["kind"], e["sha"]) for e in run["revision_history"]],
                                 [(0, "review", sha)])
                self.assertIn('"evidence": "Bug"', run["feedback"])
                self.assertEqual(run["outbox"], [])
                self.assertIn((7, f"{run['id']}-review-0-{sha}"), self.github.comments)
                pull = self.github.pr(None, 7)
                self.assertEqual((pull["head"]["sha"], pull["base"]["sha"]), (head, base))
                if stage == "handoff":
                    self.assertEqual(len(run["handoffs"]), 1)
                    with self.assertRaises(TeamError):  # a decision is required now
                        self.team.refresh(run["id"])
                    self.assertEqual(self.tick()["stage"], "waiting")
                else:
                    self.assertTrue(run["needs_revision"])
                    self.tick()  # the revision is authored, never a second review of the rejected commit
                self.assertNotIn(("claude", "review"), self.agents.calls[calls:])

    def test_refresh_after_interrupted_rejection_and_close_keeps_run_closed(self):
        self.project["max_revisions"] = 1
        self.store.save_project(self.project)
        self.agents.reject = True
        self.tick(4)
        with patch.object(self.team, "revise", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.tick()
        run = self.store.runs()[0]
        self.assertEqual((run["stage"], run["review_sha"]), ("blocked", run["sha"]))
        self.command("close", run["id"])
        calls = len(self.agents.calls)
        with self.assertRaises(TeamError):
            self.team.refresh(run["id"])
        run = self.store.get(run["id"])
        self.assertEqual(run["stage"], "closed")
        self.assertNotIn(run["sha"], run.get("rejected_shas", []))
        self.assertFalse(self.team.finalize_rejection(self.project, run))
        self.tick(2)
        self.assertEqual(self.store.get(run["id"])["stage"], "closed")
        self.assertEqual(len(self.agents.calls), calls)

    def test_interrupted_decision_publication_is_retried(self):
        run = self.exhaust()
        real = self.github.comment
        def fail_decision(repo, number, marker, *args, **kwargs):
            if "-decision-" in marker:
                raise TeamError("GitHub unavailable")
            return real(repo, number, marker, *args, **kwargs)
        with patch.object(self.github, "comment", side_effect=fail_decision):
            with self.assertRaises(TeamError):
                self.team.decide(run["id"], "extend", 1)
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], len(run["decisions"]), len(run["outbox"])), ("implement", 1, 1))
        self.tick()
        self.assertIn((7, f"{run['id']}-decision-1"), self.github.comments)
        self.assertEqual(self.store.get(run["id"])["outbox"], [])

    def test_handoff_cli_shows_record(self):
        import contextlib
        import io
        run = self.exhaust()
        with self.assertRaises(TeamError):
            self.command("handoff", "missing")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.command("handoff", run["id"])
        self.assertIn("revision limit reached", output.getvalue())
        self.assertIn("Current stage: handoff", output.getvalue())

    def test_classification_marks_uncertainty(self):
        from agent_team.coordinator import classify
        earlier = [{"location": "a.py:1", "request": "Fix X"}]
        current = [{"location": "A.py:1", "request": "fix  x"}, {"location": "a.py:9", "request": "Other"},
                   {"location": "b.py", "request": "Fix X"}]
        self.assertEqual([f["match"] for f in classify(current, earlier)], ["repeated", "uncertain", "new"])
        self.assertEqual([f["match"] for f in classify(current, [])], ["first"] * 3)

    def test_validation_failure_never_publishes(self):
        self.project.update(tests=["exit 1"], max_revisions=0)
        self.store.save_project(self.project)
        run = self.tick(3)
        self.assertEqual(run["stage"], "handoff")
        self.assertEqual(self.github.creates, 0)
        handoff = self.github.comments[(1, f"{run['id']}-handoff-0")]  # no PR: the issue gets the handoff
        for text in (run["sha"], "local only; never pushed", "`exit 1` exit 1", "no PR yet",
                     "direct repair of the candidate in a local repair checkout"):
            self.assertIn(text, handoff)

    def test_interrupted_validation_failure_is_recorded_not_rerun(self):
        for recover in ("resume", "refresh"):
            for limit, stage, round_ in ((0, "handoff", 0), (1, "implement", 1)):
                with self.subTest(recover=recover, limit=limit):
                    self.tearDown()
                    self.setUp()
                    marker = self.root / "validation-runs"
                    # Fails on its first run and passes on any rerun, like a nondeterministic command.
                    self.project.update(tests=[f"echo run >> '{marker}'; test $(wc -l < '{marker}') -gt 1"],
                                        max_revisions=limit)
                    self.store.save_project(self.project)
                    self.tick(2)
                    with patch.object(self.team, "revise", side_effect=KeyboardInterrupt()):
                        with self.assertRaises(KeyboardInterrupt):
                            self.tick()
                    self.tick()
                    run = self.store.runs()[0]
                    sha = run["sha"]
                    self.assertEqual((run["stage"], run["resume_stage"]), ("blocked", "validate"))
                    self.assertEqual(run["validation_failure"]["sha"], sha)
                    self.assertNotIn(sha, run.get("rejected_shas", []))
                    if recover == "resume":
                        self.command("resume", run["id"])
                        self.tick()
                    else:
                        self.team.refresh(run["id"])
                    run = self.store.get(run["id"])
                    self.assertEqual((run["stage"], run["round"]), (stage, round_))
                    self.assertEqual(marker.read_text().count("run"), 1)  # validation was not rerun
                    self.assertIn(sha, run["rejected_shas"])
                    self.assertEqual([(e["round"], e["kind"], e["sha"], e["tests"][0]["exit_code"])
                                      for e in run["revision_history"]], [(0, "validation", sha, 1)])
                    self.assertEqual(run["tests"][0]["exit_code"], 1)
                    self.assertIn("Validation failed", run["feedback"])
                    self.assertEqual(self.github.creates, 0)
                    if stage == "handoff":
                        self.assertEqual(len(run["handoffs"]), 1)
                        self.assertIn((1, f"{run['id']}-handoff-0"), self.github.comments)
                        self.assertEqual(self.tick()["stage"], "waiting")
                    else:
                        self.assertTrue(run["needs_revision"])
                        if recover == "refresh":
                            self.assertIsNone(run.get("resume_stage"))
                    self.assertEqual(marker.read_text().count("run"), 1)

    def exhaust_unpublished(self):
        """Validation rejects the only allowed revision, so the handoff has no PR or pushed commit."""
        self.project.update(tests=["grep -q repaired feature.txt"], max_revisions=0)
        self.store.save_project(self.project)
        run = self.tick(3)
        self.assertEqual((run["stage"], run.get("published_sha"), self.github.creates), ("handoff", None, 0))
        return run

    def commit_local(self, checkout, message="Human repair", text="repaired\n"):
        (checkout / "feature.txt").write_text(text)
        git(checkout, "add", "--all")
        git(checkout, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", message)
        return git(checkout, "rev-parse", "HEAD")

    def test_unpublished_direct_repair_is_validated_before_publication_and_reviewed(self):
        run = self.exhaust_unpublished()
        rejected = run["sha"]
        self.command("decide", run["id"], "repair", "--note", "I will fix it")
        run = self.store.get(run["id"])
        checkout = Path(run["repair_checkout"]["path"])
        self.assertEqual((run["stage"], run["decisions"][0]["checkout"]), ("repair", str(checkout)))
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), rejected)
        self.assertIn("unpublished candidate", self.github.comments[(1, f"{run['id']}-decision-1")])
        self.assertEqual(self.tick()["stage"], "waiting")
        with self.assertRaises(TeamError):  # still the rejected commit
            self.team.adopt(run["id"], ["human"])
        (checkout / "feature.txt").write_text("repaired\n")
        with self.assertRaises(TeamError):  # only an exact commit is adopted
            self.team.adopt(run["id"], ["human"])
        head = self.commit_local(checkout)
        with self.assertRaises(TeamError):  # the reviewer's family cannot review its own repair
            self.team.adopt(run["id"], ["anthropic"])
        self.command("adopt", run["id"], "--contributor", "human")
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"], run["round"], run["repair_checkout"]), ("validate", head, 1, None))
        self.assertEqual((run["adoptions"][0]["families"], run["adoptions"][0]["local"]), (["openai"], True))
        self.assertIn("human", run["contributors"])
        self.assertEqual(len(run["revision_history"]), 1)
        self.assertIn(rejected, run["rejected_shas"])
        self.assertIn("Declared contributors: human", self.github.comments[(1, f"{run['id']}-adopt-1")])
        # Nothing is pushed or published before validation.
        self.assertEqual((self.github.creates, self.github.statuses), (0, []))
        calls = len(self.agents.calls)
        run = self.tick(4)
        self.assertEqual((run["stage"], run["reviewed_sha"], run["published_sha"]), ("ready", head, head))
        self.assertEqual(self.agents.calls[calls:], [("claude", "review")])
        self.assertEqual(git(self.remote, "rev-parse", run["branch"]), head)
        self.assertIn(f"Adopted direct repair `{head}`", self.github.pull["body"])
        self.assertIn("declared contributors human", self.github.pull["body"])

    def test_unpublished_repair_refuses_rewritten_history_and_reviewer_trailers(self):
        run = self.exhaust_unpublished()
        self.team.decide(run["id"], "repair")
        checkout = Path(self.store.get(run["id"])["repair_checkout"]["path"])
        git(checkout, "reset", "--hard", run["base_sha"])
        self.commit_local(checkout, "Rewritten")
        with self.assertRaises(TeamError):
            self.team.adopt(run["id"], ["human"])
        git(checkout, "reset", "--hard", run["sha"])
        self.commit_local(checkout, "Repair\n\nAgent-Family: anthropic")
        with self.assertRaises(TeamError):
            self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run.get("adoptions"), self.github.creates), ("repair", None, 0))
        self.team.decide(run["id"], "stop")
        self.assertEqual(self.store.get(run["id"])["stage"], "closed")

    def test_interrupted_unpublished_repair_decision_and_adoption_recover(self):
        run = self.exhaust_unpublished()
        with patch.object(self.team, "queue_writes", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.team.decide(run["id"], "repair")
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run.get("decisions")), ("handoff", None))
        self.team.decide(run["id"], "repair")
        run = self.store.get(run["id"])
        head = self.commit_local(Path(run["repair_checkout"]["path"]))
        # Stop after the checkout swap, before the adoption is saved.
        with patch("agent_team.coordinator.metadata", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run.get("adoptions"), run["sha"]), ("repair", None, run["handoffs"][0]["candidate"]))
        real = self.github.comment
        def fail_adopt(repo, number, marker, *args, **kwargs):
            if "-adopt-" in marker:
                raise TeamError("GitHub unavailable")
            return real(repo, number, marker, *args, **kwargs)
        with patch.object(self.github, "comment", side_effect=fail_adopt):
            with self.assertRaises(TeamError):
                self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run["sha"], len(run["adoptions"])), ("validate", head, 1))
        self.assertEqual([w["marker"] for w in run["outbox"]], [f"{run['id']}-adopt-1"])
        calls = len(self.agents.calls)
        run = self.tick(4)
        self.assertEqual((run["stage"], run["reviewed_sha"], run["outbox"]), ("ready", head, []))
        self.assertIn((1, f"{run['id']}-adopt-1"), self.github.comments)
        self.assertEqual(self.agents.calls[calls:], [("claude", "review")])

    def advance_base(self, name="base.txt", text="base\n"):
        work = self.root / f"base-{time.time_ns()}"
        execute(["git", "clone", "--branch", "main", str(self.remote), str(work)])
        (work / name).write_text(text)
        git(work, "add", "--all")
        git(work, "-c", "user.name=Human", "-c", "user.email=human@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-m", "Base moved\n\nAgent-Family: anthropic")
        git(work, "push", "origin", "HEAD")
        return git(work, "rev-parse", "HEAD")

    def test_repair_decision_checks_author_metadata_before_cloning(self):
        run = self.exhaust_unpublished()
        config = self.store.workspace(run) / ".git" / "config"
        config.write_text(config.read_text() + "[core]\n\tfsmonitor = touch-owned\n")
        with patch("agent_team.coordinator.execute") as clone:
            with self.assertRaises(TeamError):
                self.team.decide(run["id"], "repair")
            clone.assert_not_called()
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run.get("decisions"), run.get("repair_checkout")), ("handoff", None, None))
        self.assertEqual(list(self.store.workspace(run).parent.glob("repair-*")), [])

    def test_unpublished_repair_integrates_moved_base_before_validation(self):
        run = self.exhaust_unpublished()
        self.team.decide(run["id"], "repair")
        run = self.store.get(run["id"])
        head = self.commit_local(Path(run["repair_checkout"]["path"]))
        # The base moves during the handoff; its trailers are not the repair's contributors.
        base = self.advance_base()
        self.team.adopt(run["id"], ["human"])
        run = self.store.get(run["id"])
        sha = run["sha"]
        self.assertNotEqual(sha, head)
        self.assertEqual((run["stage"], run["base_sha"]), ("validate", base))
        self.assertEqual({k: run["adoptions"][0][k] for k in ("head", "sha", "base_sha")},
                         {"head": head, "sha": sha, "base_sha": base})
        for parent in (head, base):
            git(self.store.workspace(run), "merge-base", "--is-ancestor", parent, sha)
        self.assertIn(f"merges current base `{base}`", self.github.comments[(1, f"{run['id']}-adopt-1")])
        self.assertEqual(self.github.creates, 0)
        run = self.tick(4)
        self.assertEqual((run["stage"], run["validated_sha"], run["published_sha"], run["reviewed_sha"]),
                         ("ready", sha, sha, sha))
        self.assertEqual(git(self.remote, "rev-parse", run["branch"]), sha)

    def test_unpublished_repair_conflicting_with_moved_base_keeps_repair_checkout(self):
        run = self.exhaust_unpublished()
        self.team.decide(run["id"], "repair")
        run = self.store.get(run["id"])
        checkout = Path(run["repair_checkout"]["path"])
        head = self.commit_local(checkout)
        self.advance_base("feature.txt", "conflicting\n")
        with self.assertRaises(TeamError) as caught:
            self.team.adopt(run["id"], ["human"])
        self.assertIn("conflicts with current base", str(caught.exception))
        run = self.store.get(run["id"])
        self.assertEqual((run["stage"], run.get("adoptions"), run["repair_checkout"]["path"]),
                         ("repair", None, str(checkout)))
        self.assertEqual((git(checkout, "rev-parse", "HEAD"), git(checkout, "status", "--porcelain")), (head, ""))
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
        self.store.db.execute("DELETE FROM meta WHERE key LIKE 'quota-%'")
        self.store.db.commit()
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
        self.assertEqual(self.store.get(run["id"])["stage"], "stale")
        self.assertEqual(self.github.statuses[-1], ("a" * 40, "pending"))

    def test_merged_pr_is_observed_not_reopened(self):
        run = self.tick(6)
        self.github.pull.update(state="closed", merged=True)
        self.tick()
        self.assertEqual(self.store.get(run["id"])["stage"], "merged")

    def test_stale_pr_is_still_observed_when_merged(self):
        run = self.tick(6)
        self.store.save(run, stage="stale")
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

    def test_unapproved_issue_is_not_claimed(self):
        self.github.items[0]["approved"] = False
        self.assertEqual(self.tick()["stage"], "idle")
        self.assertEqual(self.store.runs(), [])

    def test_issue_edit_after_approval_blocks(self):
        self.tick()
        self.github.items[0]["body"] = "Different task"
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Issue content changed", run["error"])

    def test_ignored_author_file_cannot_satisfy_validation(self):
        run = self.tick(2)
        author = self.store.workspace(run)
        (author / ".gitignore").write_text("hidden.txt\n")
        (author / "hidden.txt").write_text("not committed")
        self.project.update(tests=["test -f hidden.txt"], max_revisions=0)
        self.store.save_project(self.project)
        run = self.tick()
        self.assertEqual(run["stage"], "handoff")
        self.assertEqual(self.github.creates, 0)

    def test_git_config_tamper_stops_before_publication(self):
        run = self.tick(3)
        config = self.store.workspace(run) / ".git/config"
        with config.open("a") as handle:
            handle.write('\n[url "https://example.invalid/"]\n\tinsteadOf = https://github.com/\n')
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Git configuration changed", run["error"])
        self.assertEqual(self.github.creates, 0)

    def test_validation_config_tamper_stops_before_git_command(self):
        self.project["tests"] = ["printf '\\n[core]\\nfsmonitor = bad-command\\n' >> .git/config"]
        self.store.save_project(self.project)
        run = self.tick(3)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Git configuration changed", run["error"])

    def test_push_timeout_after_remote_acceptance_is_recoverable(self):
        self.agents.reject = True
        run = self.tick(7)  # revised candidate validated, old PR still published
        def timeout_after_push(cwd, *args):
            args = list(args)
            if "push" in args:
                args[args.index("push") + 1] = str(self.remote)
                git(cwd, *args)
                raise TeamError("Simulated lost push response")
            return git(cwd, *args)
        with patch("agent_team.coordinator.git", side_effect=timeout_after_push):
            run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertEqual(run["pending_push_sha"], run["sha"])
        self.store.save(run, stage=run["resume_stage"], error=None)
        run = self.tick(3)
        self.assertEqual(run["stage"], "ready")
        self.assertEqual(self.github.creates, 1)

    def test_no_change_revision_cannot_reroll_review(self):
        self.agents.reject = True
        self.tick(5)
        run = self.store.runs()[0]
        self.store.save(run, stage="validate")  # a worker returns without editing
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("no new commit", run["error"])

    def test_failed_revision_status_targets_only_published_commit(self):
        self.agents.reject = True
        self.tick(6)  # revised files exist; new candidate not yet published
        self.project.update(tests=["exit 1"], max_revisions=1)
        self.store.save_project(self.project)
        real_status = self.github.status
        def require_remote_commit(repo, sha, state, description):
            git(self.remote, "cat-file", "-e", sha + "^{commit}")
            real_status(repo, sha, state, description)
        with patch.object(self.github, "status", side_effect=require_remote_commit):
            run = self.tick()
        self.assertEqual(run["stage"], "handoff")
        self.assertNotEqual(run["sha"], run["published_sha"])
        self.assertEqual(self.github.statuses[-1], (run["published_sha"], "failure"))

    def test_repeated_quota_wait_is_bounded(self):
        self.tick()
        for attempt in range(3):
            self.agents.quota = True
            run = self.tick()
            if attempt < 2:
                self.assertEqual(run["stage"], "quota_wait")
                self.store.save(run, retry_at=time.time() - 1)
                self.store.db.execute("DELETE FROM meta WHERE key LIKE 'quota-%'")
                self.store.db.commit()
        self.store.db.execute("DELETE FROM meta WHERE key LIKE 'quota-%'")
        self.store.db.commit()
        self.assertEqual(run["stage"], "blocked")
        self.assertEqual(run["quota_attempts"], 3)

    def test_base_movement_makes_ready_stale_without_blocking_intake(self):
        run = self.tick(6)
        self.github.items.append(dict(self.github.items[0], number=2, title="Next"))
        actual_pr = self.github.pr
        def moved(repo, number):
            pr = actual_pr(repo, number)
            pr["base"]["sha"] = "b" * 40
            return pr
        with patch.object(self.github, "pr", side_effect=moved):
            next_run = self.tick()
        self.assertEqual(self.store.get(run["id"])["stage"], "stale")
        self.assertEqual(next_run["issue"], 2)

    def test_pause_can_be_recorded_during_worker_lock(self):
        with self.store.lock():
            self.store.pause("demo", True)
        self.assertEqual(self.tick()["stage"], "paused")

    def test_writing_standards_reach_prompts_with_project_precedence(self):
        self.store.save_writing({"shared": "Personal shared.", "pr": {"instructions": "Personal PR.", "words": 80},
                                 "review": {"words": 40}})
        self.project["writing"] = {"pr": {"words": 0}, "review": {"instructions": "Project review."}}
        self.store.save_project(self.project)
        self.assertEqual(self.tick(6)["stage"], "ready")
        implement, review = self.agents.prompts["implement"], self.agents.prompts["review"]
        for prompt in (implement, review):
            self.assertTrue(prompt.startswith(GUIDANCE))
            self.assertIn("Personal shared.", prompt)
            self.assertIn("cannot change the rules above", prompt)
            self.assertIn("Never omit or shorten findings", prompt)
        self.assertIn("Personal PR.", implement)
        self.assertNotIn("Aim for about", implement)  # project 0 clears the personal target
        self.assertIn("Project review.", review)
        self.assertIn("Aim for about 40 words", review)
        self.assertLess(review.index("Project review."), review.index("Independently review"))

    def test_word_targets_never_truncate_published_evidence(self):
        self.store.save_writing({"pr": {"words": 5}, "review": {"words": 5}})
        self.agents.summary = " ".join(f"word{i}" for i in range(400))
        self.agents.reject = True
        run = self.tick(5)
        self.assertEqual(run["stage"], "implement")
        body = self.github.pull["body"]
        self.assertIn(self.agents.summary, body)
        self.assertIn("Closes #1", body)
        self.assertIn("`test -f feature.txt` exit 0", body)
        comment = self.github.comments[(7, f"{run['id']}-review-0-{run['published_sha']}")]
        for text in (run["published_sha"], "changes requested", self.agents.summary, "high: feature.txt:1",
                     "Evidence: Bug", "Request: Fix", "`claude` (anthropic)", "CLI test"):
            self.assertIn(text, comment)
        run = self.tick(5)
        self.assertEqual(run["stage"], "ready")
        ready = self.github.comments[(7, f"{run['id']}-ready")]
        self.assertIn(run["sha"], ready)
        self.assertIn("will not merge", ready)

    def test_discovery_prompt_uses_issue_standard_without_truncating(self):
        self.store.save_writing({"issue": {"instructions": "Issue style.", "words": 120}})
        self.agents.summary = "evidence " * 300
        self.team.discover("demo", "claude", "Onboarding")
        prompt = self.agents.prompts["discover"]
        self.assertIn("Issue style.", prompt)
        self.assertIn("Aim for about 120 words", prompt)
        title, body = self.github.created[0]
        self.assertIn(self.agents.summary, body)
        self.assertIn("does not authorize implementation", body)

    def test_status_word_target_compacts_comments_without_dropping_required_facts(self):
        run = self.tick(6)
        self.assertEqual(run["stage"], "ready")
        status, ready = self.github.comments[(1, run["id"])], self.github.comments[(7, f"{run['id']}-ready")]
        self.assertIn("author `codex` (openai)", status)  # built-in: no target, detailed
        self.assertIn("configured local validation", ready)
        self.store.save_writing({"status": {"words": 10}})
        self.team.notify(self.project, run)
        self.store.save(run, stage="ci")
        self.tick()
        compact_status = self.github.comments[(1, run["id"])]
        compact_ready = self.github.comments[(7, f"{run['id']}-ready")]
        self.assertNotIn("author `codex`", compact_status)
        self.assertNotIn("configured local validation", compact_ready)
        self.assertLess(len(compact_status.split()), len(status.split()))
        for text in ("Agent Team: ready", run["id"], "PR #7", run["sha"], "Only the maintainer decides whether to merge."):
            self.assertIn(text, compact_status)
        for text in (run["sha"], "independent review", "`test -f feature.txt` exit 0", "will not merge"):
            self.assertIn(text, compact_ready)
        self.project["writing"] = {"status": {"words": 0}}  # project override clears the personal target
        self.store.save_project(self.project)
        self.team.notify(self.project, self.store.get(run["id"]))
        self.assertEqual(self.github.comments[(1, run["id"])], status)

    def test_status_instructions_shape_published_comments_and_keep_fixed_facts(self):
        self.store.save_writing({"shared": "Personal shared.", "status": {"instructions": "Write in Spanish."}})
        run = self.tick(6)
        self.assertEqual(run["stage"], "ready")
        self.assertIn(("codex", "status"), self.agents.calls)
        prompt = self.agents.prompts["status"]
        self.assertTrue(prompt.startswith(GUIDANCE))
        self.assertIn("Write in Spanish.", prompt)
        self.assertIn("Do not add facts", prompt)
        status, ready = self.github.comments[(1, run["id"])], self.github.comments[(7, f"{run['id']}-ready")]
        for comment in (status, ready):
            self.assertTrue(comment.startswith("Drafted: Personal shared. Write in Spanish."))
        for text in ("Agent Team: ready", run["id"], "PR #7", run["sha"], "Only the maintainer decides whether to merge."):
            self.assertIn(text, status)
        for text in (run["sha"], "independent review", "`test -f feature.txt` exit 0", "will not merge"):
            self.assertIn(text, ready)
        calls = len(self.agents.calls)
        self.team.notify(self.project, run)  # unchanged status reuses the stored draft
        self.assertEqual(len(self.agents.calls), calls)
        self.assertEqual(self.github.comments[(1, run["id"])], status)
        self.project["writing"] = {"status": {"instructions": "Project status."}}
        self.store.save_project(self.project)
        self.team.notify(self.project, run)
        self.assertTrue(self.github.comments[(1, run["id"])].startswith("Drafted: Personal shared. Project status."))
        self.assertIn(run["sha"], self.github.comments[(1, run["id"])])

    def test_status_drafting_failure_or_quota_wait_publishes_template(self):
        self.store.save_writing({"status": {"instructions": "Write in Spanish."}})
        real_run = self.agents.run
        def no_status(agent, role, *args):
            if role == "status":
                raise QuotaError("quota exhausted")
            return real_run(agent, role, *args)
        with patch.object(self.agents, "run", side_effect=no_status):
            run = self.tick()
        self.assertEqual(run["stage"], "implement")
        comment = self.github.comments[(1, run["id"])]
        self.assertTrue(comment.startswith("**Agent Team: implement**"))
        self.agents.quota = True
        run = self.tick()
        self.assertEqual(run["stage"], "quota_wait")
        self.assertNotIn(("codex", "status"), self.agents.calls)
        self.assertTrue(self.github.comments[(1, run["id"])].startswith("**Agent Team: quota_wait**"))

    def test_failed_status_draft_is_not_retried_during_pending_ci(self):
        run = self.tick(5)
        self.github.check_state = "pending"
        self.store.save_writing({"status": {"instructions": "Write in Spanish."}})
        for outcome in (TeamError("failed"), QuotaError("quota exhausted"),
                        {"report": {"message": " "}}):
            with self.subTest(outcome=outcome):
                # Isolate each outcome from the preceding shared quota cooldown.
                self.store.db.execute("DELETE FROM meta WHERE key LIKE 'quota-%'")
                self.store.db.commit()
                # A new policy is a distinct update, allowing one new attempt.
                self.store.save_writing({"status": {"instructions": str(outcome)}})
                kwargs = ({"side_effect": outcome} if isinstance(outcome, Exception)
                          else {"return_value": outcome})
                with patch.object(self.agents, "run", **kwargs) as call:
                    for _ in range(3):
                        run = self.tick()
                        self.assertEqual(run["stage"], "ci")
                    self.assertEqual(call.call_count, 1)
                self.assertTrue(self.github.comments[(1, run["id"])].startswith("**Agent Team: ci**"))
                self.assertEqual(run["status_drafts"]["status"]["state"], "fallback")

    def test_interrupted_status_attempt_is_persisted_and_not_repeated(self):
        run = self.tick(5)
        self.github.check_state = "pending"
        self.store.save_writing({"status": {"instructions": "Write in Spanish."}})

        def interrupt(*args):
            saved = self.store.get(run["id"])
            self.assertEqual(saved["status_drafts"]["status"]["state"], "attempted")
            raise KeyboardInterrupt()

        with patch.object(self.agents, "run", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        # Reopen durable storage, as after a process restart.
        self.store.db.close()
        self.store = Store(self.root / "state")
        self.team = Coordinator(self.store, self.github, self.agents)
        with patch.object(self.agents, "run") as call:
            run = self.tick(3)
            call.assert_not_called()
        self.assertEqual(run["stage"], "ci")
        self.assertTrue(self.github.comments[(1, run["id"])].startswith("**Agent Team: ci**"))
        self.assertEqual(run["status_drafts"]["status"]["state"], "fallback")

    def test_compact_status_keeps_waiting_notice(self):
        self.store.save_writing({"status": {"words": 1}})
        self.tick()
        self.github.items[0]["labels"] = []
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        comment = self.github.comments[(1, run["id"])]
        self.assertIn("Agent Team: blocked", comment)
        self.assertIn("Waiting for local operator action", comment)
        self.assertIn("Only the maintainer decides", comment)

    def test_invalid_writing_settings_block_before_agent_call(self):
        self.tick()
        self.project["writing"] = {"pr": {"words": -1}}
        self.store.save_project(self.project)
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertEqual(self.agents.calls, [])
        self.assertIn("Agent Team: blocked", self.github.comments[(1, run["id"])])


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

    def test_non_utf8_output_is_preserved_with_replacement(self):
        result = execute(["/bin/sh", "-c", "printf '\\377'"])
        self.assertIn("\ufffd", result.stdout)

    def test_keyboard_interrupt_stops_watcher_instead_of_becoming_task_failure(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.pid = 12345
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch("agent_team.process.subprocess.Popen", return_value=process), \
                patch("agent_team.process.os.killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                execute(["fake-worker"])
            kill.assert_called_once()

    def test_interrupt_still_propagates_if_process_group_already_exited(self):
        from unittest.mock import MagicMock
        process = MagicMock()
        process.pid = 12345
        process.communicate.side_effect = [KeyboardInterrupt(), ("", "")]
        with patch("agent_team.process.subprocess.Popen", return_value=process), \
                patch("agent_team.process.os.killpg", side_effect=ProcessLookupError):
            with self.assertRaises(KeyboardInterrupt):
                execute(["fake-worker"])

    def test_duplicate_registration_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            store.register("one", "example/repo", "main", ["true"])
            with self.assertRaises(TeamError):
                store.register("one", "example/other", "main", ["true"])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
