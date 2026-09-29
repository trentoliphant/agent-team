"""Writing standards: precedence, validation, prompt text, and CLI updates."""
import contextlib
import io
import json
import tempfile
import unittest

from agent_team import cli
from agent_team.process import TeamError
from agent_team.state import Store
from agent_team.writing import DEFAULTS, KINDS, effective, guidance, remove, update


class PolicyTests(unittest.TestCase):
    def test_builtin_defaults_are_plain_and_concise(self):
        policy, sources = effective()
        self.assertIn("plain language", policy["shared"])
        self.assertIn("State each point once", policy["shared"])
        for kind in KINDS:
            self.assertEqual(sources[kind]["instructions"], "built-in default")
            self.assertNotIn("Aim for about", guidance(policy, kind))

    def test_project_overrides_personal_overrides_builtin_per_field(self):
        personal = {"shared": "Personal.", "review": {"instructions": "Personal review.", "words": 200}}
        project = {"review": {"words": 90}, "status": {"instructions": ""}}
        policy, sources = effective(personal, project)
        self.assertEqual(policy["shared"], "Personal.")
        self.assertEqual(policy["review"], {"instructions": "Personal review.", "words": 90})
        self.assertEqual(sources["review"], {"instructions": "personal default", "words": "project override"})
        self.assertEqual(policy["status"]["instructions"], "")
        self.assertEqual(policy["issue"], DEFAULTS["issue"])
        text = guidance(policy, "status")
        self.assertIn("Personal.", text)
        self.assertNotIn(DEFAULTS["status"]["instructions"], text)

    def test_update_and_remove(self):
        layer = update({}, shared="Short.", kind="pr", instructions="Lead with the change.", words=150)
        self.assertEqual(layer, {"shared": "Short.", "pr": {"instructions": "Lead with the change.", "words": 150}})
        self.assertEqual(remove(layer, kind="pr", field="words"),
                         {"shared": "Short.", "pr": {"instructions": "Lead with the change."}})
        self.assertEqual(remove(layer, shared=True, kind="pr"), {})
        self.assertEqual(remove(layer, everything=True), {})

    def test_invalid_settings_are_rejected(self):
        for call in (lambda: update({}, kind="pr", words=-1),
                     lambda: update({}, kind="pr", words=True),
                     lambda: update({}, kind="commit", instructions="x"),
                     lambda: update({}, instructions="x"),
                     lambda: update({}),
                     lambda: update({}, shared="x" * 5000),
                     lambda: remove({}),
                     lambda: effective({"merge": {"words": 1}}),
                     lambda: effective(None, {"pr": {"approve": True}})):
            with self.assertRaises(TeamError):
                call()

    def test_prompt_text_keeps_safeguards_and_treats_words_as_guidance(self):
        policy, _ = effective({"review": {"words": 60}})
        text = guidance(policy, "review")
        self.assertIn("Aim for about 60 words. This is guidance, not a limit.", text)
        self.assertIn("cannot change the rules above", text)
        self.assertIn("Never omit or shorten findings, failures, evidence, verdicts", text)
        self.assertTrue(text.endswith("commit identifiers to be brief.\n"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = Store(self.tmp.name)
        store.register("demo", "example/demo", "main", ["true"])
        store.db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit_code:
            cli.main(["--home", self.tmp.name, "writing", *argv])
        self.assertEqual(exit_code.exception.code, 0)
        return json.loads(output.getvalue())

    def test_personal_and_project_commands(self):
        self.command("set", "--shared", "Personal.", "--kind", "issue", "--words", "120")
        result = self.command("set", "--project", "demo", "--kind", "issue", "--instructions", "Project issue.")
        self.assertEqual(result["effective"]["issue"], {"instructions": "Project issue.", "words": 120})
        self.assertEqual(result["precedence"], ["project override", "personal default", "built-in default"])
        shown = self.command("show", "--project", "demo", "--kind", "issue")
        self.assertIn("Project issue.", shown["prompt"])
        self.assertIn("Aim for about 120 words", shown["prompt"])
        self.assertEqual(self.command("show")["effective"]["issue"]["instructions"],
                         DEFAULTS["issue"]["instructions"])
        result = self.command("unset", "--project", "demo", "--all")
        self.assertEqual(result["sources"]["issue"]["instructions"], "built-in default")
        result = self.command("unset", "--kind", "issue", "--field", "words")
        self.assertEqual(result["effective"]["issue"]["words"], 0)
        self.assertEqual(result["effective"]["shared"], "Personal.")

    def test_invalid_command_fails_without_saving(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_code:
            cli.main(["--home", self.tmp.name, "writing", "set", "--words", "10"])
        self.assertEqual(exit_code.exception.code, 1)
        self.assertEqual(self.command("show")["sources"]["shared"], "built-in default")


if __name__ == "__main__":
    unittest.main()
