import json
import os
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
        for text in (run["sha"], "local only; never pushed", "`exit 1` exit 1", "no PR yet"):
            self.assertIn(text, handoff)

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
                store.register("two", "example/repo", "main", ["true"])
            store.db.close()


if __name__ == "__main__":
    unittest.main()
