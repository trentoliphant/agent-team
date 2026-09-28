import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_team.agents import Agents
from agent_team.github import GitHub
from agent_team.process import TeamError, QuotaError


class AdapterTests(unittest.TestCase):
    def call(self, agent, payload, *, exit_code=0, stderr="", role="review"):
        self.commands = []
        self.environments = []
        report = {"verdict": "pass", "summary": "ok", "findings": []}

        def fake_execute(args, **kwargs):
            self.commands.append(args)
            self.environments.append(kwargs.get("env", {}))
            if args[0] == "codex":
                Path(args[args.index("-o") + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(args, exit_code, json.dumps(payload), stderr)

        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_team.agents.subscription_status", return_value="test-version"), \
                    patch("agent_team.agents.execute", side_effect=fake_execute):
                return Agents().run(agent, role, "Review", Path(temp), Path(temp) / "artifacts", {"timeout": 30})

    def test_codex_uses_subscription_and_read_only_sandbox(self):
        result = self.call("codex", {"type": "turn.completed"})
        command = self.commands[-1]
        self.assertIn('forced_login_method="chatgpt"', command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertEqual(result["family"], "openai")

    def test_codex_failed_event_even_with_zero_exit_is_rejected(self):
        with self.assertRaises(TeamError):
            self.call("codex", {"type": "turn.failed", "error": {"message": "Failure"}})

    def test_codex_quota_error_is_retryable(self):
        with self.assertRaises(QuotaError):
            self.call("codex", {"type": "error", "message": "Usage limit reached"})

    def test_transcript_mention_of_rate_limit_does_not_trigger_retry(self):
        with self.assertRaises(TeamError) as raised:
            self.call("codex", {"type": "item.completed", "item": {"text": "Implement rate limit handling"}},
                      exit_code=1, stderr="Process failed")
        self.assertNotIsInstance(raised.exception, QuotaError)

    def test_claude_review_has_no_shell_or_edit_tools(self):
        result = self.call("claude", {"is_error": False, "modelUsage": {"a-model": {}},
                                     "structured_output": {"verdict": "pass", "summary": "ok", "findings": []}})
        command = self.commands[-1]
        self.assertIn("--restricted", command)
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--bare", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(result["observed_models"], ["a-model"])

    def test_claude_quota_error_inside_successful_process(self):
        with self.assertRaises(QuotaError):
            self.call("claude", {"is_error": True, "result": "You've hit your limit"})

    def test_claude_missing_structured_output_fails_closed(self):
        with self.assertRaises(TeamError):
            self.call("claude", {"is_error": False, "result": "Looks good!"})

    def test_passing_review_with_findings_rejected(self):
        with self.assertRaises(TeamError):
            self.call("claude", {"structured_output": {"verdict": "pass", "summary": "ok", "findings": [
                {"severity": "high", "location": "a:1", "evidence": "bad", "request": "fix"}]}})


class GitHubTests(unittest.TestCase):
    def test_approval_requires_current_content_and_trusted_identity(self):
        from agent_team.state import issue_fingerprint
        github = GitHub()
        github._login = "operator"
        issue = {"number": 1, "title": "Task", "body": "Original"}
        comment = {"body": f"<!-- agent-team:approval-1 -->\nIssue content SHA-256: `{issue_fingerprint(issue)}`",
                   "user": {"login": "operator"}}
        with patch.object(github, "pages", return_value=[comment]):
            self.assertTrue(github.authorized({"repo": "example/repo"}, issue))
            self.assertFalse(github.authorized({"repo": "example/repo"}, dict(issue, body="Edited")))
            comment["user"]["login"] = "outsider"
            self.assertFalse(github.authorized({"repo": "example/repo"}, issue))

    def test_identity_uses_graphql_viewer_for_app_compatibility(self):
        github = GitHub()
        with patch.object(github, "api", return_value={"data": {"viewer": {"login": "team[bot]"}}}) as api:
            self.assertEqual(github.login(), "team[bot]")
            self.assertEqual(github.login(), "team[bot]")
            self.assertEqual(api.call_count, 1)
            self.assertEqual(api.call_args.args[0], "graphql")

    def test_own_status_does_not_deadlock_ci(self):
        github = GitHub()
        with patch.object(github, "api", side_effect=[
            {"total_count": 0, "check_runs": []},
            {"total_count": 1, "statuses": [{"context": "agent-team/review", "state": "pending"}]},
        ]):
            self.assertEqual(github.ci("example/repo", "a" * 40), "success")

    def test_incomplete_checks_wait(self):
        github = GitHub()
        with patch.object(github, "api", side_effect=[
            {"total_count": 1, "check_runs": [{"status": "in_progress", "conclusion": None}]},
            {"total_count": 0, "statuses": []},
        ]):
            self.assertEqual(github.ci("example/repo", "a" * 40), "pending")

    def test_failed_check_blocks(self):
        github = GitHub()
        with patch.object(github, "api", side_effect=[
            {"total_count": 1, "check_runs": [{"status": "completed", "conclusion": "failure"}]},
            {"total_count": 0, "statuses": []},
        ]):
            self.assertEqual(github.ci("example/repo", "a" * 40), "failure")

    def test_comment_retry_updates_only_own_marked_comment(self):
        github = GitHub()
        github._login = "coordinator-bot"
        comments = [{"id": 1, "body": "<!-- agent-team:run -->", "user": {"login": "outsider"}},
                    {"id": 2, "body": "<!-- agent-team:run -->", "user": {"login": "coordinator-bot"}}]
        with patch.object(github, "pages", return_value=comments), patch.object(github, "api") as api:
            github.comment("example/repo", 1, "run", "Updated status")
            self.assertEqual(api.call_args.args[0], "repos/example/repo/issues/comments/2")
            self.assertEqual(api.call_args.args[1], "PATCH")

    def test_publication_reuses_existing_pr(self):
        github = GitHub()
        with patch.object(github, "find_pr", return_value={"number": 9}), patch.object(github, "api") as api:
            self.assertEqual(github.create_pr({"repo": "example/repo"}, {"branch": "topic"}, "body"), {"number": 9})
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
