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
    def call(self, agent, payload, *, exit_code=0, stderr="", role="review", readable=()):
        self.commands = []
        self.environments = []
        self.workspaces = []
        report = ({"message": "Status"} if role == "status" else {"issues": []} if role == "discover" else
                  
                  {"verdict": "pass", "summary": "ok", "findings": []})

        def fake_execute(args, **kwargs):
            self.commands.append(args)
            self.workspaces.append((kwargs["cwd"], Path(kwargs["cwd"]).exists(),
                                    (Path(kwargs["cwd"]) / ".git").exists()))
            self.environments.append(kwargs.get("env", {}))
            if args[0] == "codex":
                Path(args[args.index("-o") + 1]).write_text(json.dumps(report))
            return subprocess.CompletedProcess(args, exit_code, json.dumps(payload), stderr)

        with tempfile.TemporaryDirectory() as temp:
            with patch("agent_team.agents.subscription_status", return_value="test-version"), \
                    patch("agent_team.agents.execute", side_effect=fake_execute):
                return Agents().run(agent, role, "Review", Path(temp), Path(temp) / "artifacts", {"timeout": 30},
                                    readable=readable)

    def test_codex_uses_subscription_and_a_sandbox_for_every_role(self):
        result = self.call("codex", {"type": "turn.completed"})
        command = self.commands[-1]
        self.assertIn('forced_login_method="chatgpt"', command)
        # Author and reviewer run commands in a workspace-write sandbox, which has no network access.
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertFalse([a for a in command if "network_access" in a or "danger-full-access" in a])
        self.assertEqual(result["family"], "openai")
        for role in ("discover", "status"):
            with self.subTest(role=role):
                self.call("codex", {"type": "turn.completed"}, role=role)
                self.assertEqual(self.commands[-1][self.commands[-1].index("--sandbox") + 1], "read-only")

    def test_codex_status_runs_read_only_outside_git(self):
        result = self.call("codex", {"type": "turn.completed"}, role="status")
        command = self.commands[-1]
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(self.workspaces[-1][1:], (True, False))
        self.assertEqual(result["report"], {"message": "Status"})
        self.call("codex", {"type": "turn.completed"})
        self.assertNotIn("--skip-git-repo-check", self.commands[-1])

    def test_codex_failed_event_even_with_zero_exit_is_rejected(self):
        with self.assertRaises(TeamError):
            self.call("codex", {"type": "turn.failed", "error": {"message": "Failure"}})

    def test_codex_quota_error_is_retryable(self):
        with self.assertRaises(QuotaError):
            self.call("codex", {"type": "error", "message": "Usage limit reached"})

    def test_model_at_capacity_is_retryable(self):
        message = "Selected model is at capacity. Please try a different model."
        with self.assertRaises(QuotaError):
            self.call("codex", {"type": "turn.failed", "error": {"message": message}})
        with self.assertRaises(QuotaError):
            self.call("claude", {"is_error": True, "result": message})
        # A transcript that only discusses capacity is still an ordinary failure.
        with self.assertRaises(TeamError) as raised:
            self.call("codex", {"type": "item.completed", "item": {"text": "The model is at capacity handling"}},
                      exit_code=1, stderr="Process failed")
        self.assertNotIsInstance(raised.exception, QuotaError)

    def test_transcript_mention_of_rate_limit_does_not_trigger_retry(self):
        with self.assertRaises(TeamError) as raised:
            self.call("codex", {"type": "item.completed", "item": {"text": "Implement rate limit handling"}},
                      exit_code=1, stderr="Process failed")
        self.assertNotIsInstance(raised.exception, QuotaError)

    def test_claude_review_runs_commands_only_inside_the_sandbox(self):
        result = self.call("claude", {"is_error": False, "modelUsage": {"a-model": {}},
                                     "structured_output": {"verdict": "pass", "summary": "ok", "findings": []}})
        command = self.commands[-1]
        self.assertIn("--restricted", command)
        self.assertIn("--safe-mode", command)
        self.assertNotIn("--bare", command)
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep,Edit,Write,Bash")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        sandbox = json.loads(command[command.index("--settings") + 1])["sandbox"]
        self.assertEqual((sandbox["enabled"], sandbox["allowUnsandboxedCommands"]), (True, False))
        self.assertNotIn("network", sandbox)  # no domain is allowed
        for secret in ("~/.ssh", "~/.aws", "~/.config/gh"):
            self.assertIn(secret, sandbox["filesystem"]["denyRead"])
        # One command may run as long as the call itself (30 seconds here), not Claude's two-minute default.
        environment = self.environments[-1]
        self.assertEqual((environment["BASH_DEFAULT_TIMEOUT_MS"], environment["BASH_MAX_TIMEOUT_MS"]), ("30000", "30000"))
        self.assertEqual(result["observed_models"], ["a-model"])

    def test_claude_status_has_no_shell_or_edit_tools(self):
        self.call("claude", {"is_error": False, "structured_output": {"message": "Status"}}, role="status")
        command = self.commands[-1]
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("--settings", command)
        self.assertEqual(command[command.index("--max-turns") + 1], "40")
        self.assertNotIn("BASH_MAX_TIMEOUT_MS", self.environments[-1])
        self.assertNotIn("--add-dir", command)

    def test_claude_can_read_companion_checkouts_without_new_tools(self):
        envelope = {"is_error": False, "structured_output": {"verdict": "pass", "summary": "ok", "findings": []}}
        self.call("claude", envelope)
        single = self.commands[-1]
        companions = [Path("/runs/r/review-1/lib"), Path("/runs/r/review-1/other lib")]
        self.call("claude", envelope, readable=companions)
        command = self.commands[-1]
        self.assertEqual([command[i + 1] for i, a in enumerate(command) if a == "--add-dir"],
                         [str(p) for p in companions])
        # The directory grants are the only difference; tools and permission mode are unchanged.
        extra = [a for i, a in enumerate(command) if a == "--add-dir" or (i and command[i - 1] == "--add-dir")]
        remaining = [a for a in command if a not in extra]
        self.assertEqual(remaining[:remaining.index("--json-schema")], single[:single.index("--json-schema")])
        self.assertEqual(command[command.index("--tools") + 1], "Read,Glob,Grep,Edit,Write,Bash")
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.call("claude", {"is_error": False, "structured_output": {"summary": "s", "limitations": "l", "responses": []}},
                  role="implement", readable=companions)
        self.assertEqual(self.commands[-1][self.commands[-1].index("--tools") + 1], "Read,Glob,Grep,Edit,Write,Bash")

    def test_codex_needs_no_companion_grant(self):
        self.call("codex", {"type": "turn.completed"})
        single = self.commands[-1]
        self.call("codex", {"type": "turn.completed"}, readable=[Path("/runs/r/review-1/lib")])
        self.assertEqual(self.commands[-1][:self.commands[-1].index("--output-schema")],
                         single[:single.index("--output-schema")])
        self.assertNotIn("--add-dir", self.commands[-1])

    def test_claude_quota_error_inside_successful_process(self):
        with self.assertRaises(QuotaError):
            self.call("claude", {"is_error": True, "result": "You've hit your limit"})

    def test_claude_missing_structured_output_fails_closed(self):
        with self.assertRaises(TeamError):
            self.call("claude", {"is_error": False, "result": "Looks good!"})

    def test_passing_review_with_findings_rejected(self):
        with self.assertRaisesRegex(TeamError, "unambiguous verdict"):
            self.call("claude", {"structured_output": {"verdict": "pass", "summary": "ok", "findings": [
                {"severity": "blocking", "location": "a:1", "evidence": "bad", "request": "fix"}]}})

    def test_pass_may_list_minor_findings_and_a_rejection_needs_a_blocking_one(self):
        minor = {"severity": "minor", "location": "a:1", "evidence": "style", "request": "rename"}
        accepted = self.call("claude", {"is_error": False, "structured_output": {
            "verdict": "pass", "summary": "ok", "findings": [minor]}})
        self.assertEqual(accepted["report"]["findings"], [minor])
        for findings in ([], [minor]):
            with self.subTest(findings=findings), self.assertRaisesRegex(TeamError, "unambiguous verdict"):
                self.call("claude", {"is_error": False, "structured_output": {
                    "verdict": "changes_requested", "summary": "s", "findings": findings}})

    def test_review_severity_uses_fixed_values(self):
        finding = {"severity": "P1", "location": "a:1", "evidence": "bad", "request": "fix"}
        with self.assertRaisesRegex(TeamError, "value outside"):
            self.call("claude", {"structured_output": {"verdict": "changes_requested", "summary": "s", "findings": [finding]}})


class GitHubTests(unittest.TestCase):
    def test_approval_requires_current_content_and_trusted_identity(self):
        from agent_team.state import issue_fingerprint
        github = GitHub()
        github._login = "operator"
        issue = {"number": 1, "title": "Task", "body": "Original"}
        github._identity = {"id": 1, "type": "User", "login": "operator"}
        comment = {"id": 5, "node_id": "comment5", "body": github.approval_text(issue), "created_at": "now", "updated_at": "now",
                   "user": {"id": 1, "login": "operator", "type": "User"}}
        with patch.object(github, "pages", return_value=[comment]), patch.object(github, "api", return_value={
                "data": {"node": {"body": comment["body"], "lastEditedAt": None,
                                  "author": {"__typename": "User", "databaseId": 1}}}}):
            self.assertTrue(github.authorized({"repo": "example/repo"}, issue))
            self.assertFalse(github.authorized({"repo": "example/repo"}, dict(issue, body="Edited")))
            comment["user"]["id"] = 2
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

    def test_long_review_continues_in_marked_comments_without_losing_findings(self):
        from agent_team.coordinator import review_comment
        stored = []

        def transport(args, input=None, **kwargs):
            # Stand-in for `gh api`: an in-memory issue comment thread.
            if "--paginate" in args:
                result = [[dict(c) for c in stored]]
            else:
                method, endpoint = args[args.index("--method") + 1], args[args.index("--method") + 2]
                data = json.loads(input) if input else None
                if endpoint == "graphql":
                    result = {"data": {"viewer": {"login": "coordinator-bot"}}}
                elif method == "POST":
                    self.assertEqual(endpoint, "repos/example/repo/issues/7/comments")
                    result = {"id": len(stored) + 1, "body": data["body"], "user": {"login": "coordinator-bot"}}
                    stored.append(result)
                else:
                    self.assertEqual(method, "PATCH")
                    result = next(c for c in stored if endpoint == f"repos/example/repo/issues/comments/{c['id']}")
                    result["body"] = data["body"]
            return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

        sha = "c" * 40
        findings = [{"severity": "blocking", "location": f"file.py:{n}", "evidence": f"evidence-{n} " + "x" * 30000,
                     "request": f"request-{n}"} for n in range(4)]
        record = {"agent": "claude", "family": "anthropic", "cli_version": "test", "requested_model": "test",
                  "observed_models": [], "report": {"verdict": "changes_requested", "summary": "s" * 70000,
                                                    "findings": findings}}
        body = review_comment(sha, record)
        self.assertGreater(len(body), 180000)
        github = GitHub()
        marker = f"run-review-0-{sha}"
        with patch("agent_team.github.execute", side_effect=transport):
            github.comment("example/repo", 7, marker, body, heading=f"Independent review of `{sha}`")
            self.assertGreater(len(stored), 1)
            self.assertTrue(all(len(c["body"]) <= 65536 for c in stored))
            self.assertTrue(stored[0]["body"].startswith(f"<!-- agent-team:{marker} -->\n"))
            published = stored[0]["body"].split("\n", 1)[1]
            for number, part in enumerate(stored[1:], 2):
                tag, heading, text = part["body"].split("\n", 1)[0], *part["body"].split("\n", 1)[1].split("\n\n", 1)
                self.assertEqual(tag, f"<!-- agent-team:{marker}-part-{number} -->")
                self.assertIn(sha, heading)
                self.assertIn(f"part {number} of {len(stored)}", heading)
                published += text
            self.assertEqual(published, body)
            for finding in findings:
                for field in finding.values():
                    self.assertIn(field, published)
            count = len(stored)
            github.comment("example/repo", 7, marker, "Short replacement", heading="Review")
        self.assertEqual(len(stored), count)  # updates in place; no duplicates
        self.assertTrue(stored[0]["body"].endswith("Short replacement"))
        for part in stored[1:]:
            self.assertIn("No longer used", part["body"])

    def test_publication_reuses_existing_pr(self):
        github = GitHub()
        with patch.object(github, "find_pr", return_value={"number": 9, "body": "body"}), \
                patch.object(github, "api") as api:
            self.assertEqual(github.create_pr({"repo": "example/repo"}, {"branch": "topic"}, "body"),
                             {"number": 9, "body": "body"})
            api.assert_not_called()

    def test_republication_updates_stale_pr_body(self):
        github = GitHub()
        with patch.object(github, "find_pr", return_value={"number": 9, "body": "old pins"}), \
                patch.object(github, "api", return_value={"number": 9, "body": "new pins"}) as api:
            self.assertEqual(github.create_pr({"repo": "example/repo"}, {"branch": "topic"}, "new pins"),
                             {"number": 9, "body": "new pins"})
            api.assert_called_once_with("repos/example/repo/pulls/9", "PATCH", {"body": "new pins"})


if __name__ == "__main__":
    unittest.main()
