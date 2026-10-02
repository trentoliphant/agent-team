from pathlib import Path
import subprocess
import sys
import unittest

from agent_team import evidence

ROOT = Path(__file__).resolve().parents[1]
FINDING = {"severity": "high", "location": "feature.txt:1", "evidence": "Bug", "request": "Fix"}
PATCH = {"format": "compact", "default_characters": 90000, "files": 3, "changed_lines": 40}
RECORD = {"agent": "claude", "family": "anthropic", "cli_version": "2.1", "requested_model": "opus",
          "observed_models": ["opus"], "patch": PATCH, "session": "not a reviewer field",
          "report": {"verdict": "changes_requested", "summary": "Needs a fix", "findings": [FINDING]}}
TESTS = [{"command": "make lint", "exit_code": 1, "output": "lint failed"}]
PLAN = ["make lint", "make test"]
PINS = [{"repo": "example/companion", "rev": "c" * 40}]


def run(**changes):
    return {"sha": "a" * 40, "base_sha": "b" * 40, "validated_companions": PINS, "evidence_generation": 2,
            "ci_checks": [], **changes}


class ImportTests(unittest.TestCase):
    def test_module_imports_alone_without_coordinator(self):
        code = ("import sys, agent_team.evidence; "
                "print(sorted(m for m in sys.modules if m.startswith('agent_team')))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "['agent_team', 'agent_team.evidence']")


class ReviewRecordTests(unittest.TestCase):
    def test_review_report_keeps_every_report_and_reviewer_field(self):
        report = evidence.review_report(RECORD, "a" * 40, "b" * 40, True)
        self.assertEqual(report, {
            "commit": "a" * 40, "base": "b" * 40, "current": True, "verdict": "changes_requested",
            "summary": "Needs a fix", "findings": [FINDING],
            "reviewer": {"agent": "claude", "family": "anthropic", "cli_version": "2.1",
                         "requested_model": "opus", "observed_models": ["opus"]},
            "patch": PATCH})

    def test_historical_evidence_is_never_current_and_keeps_validation(self):
        entry = {"reason": "PR base changed", "at": 12.5, "head": "a" * 40, "base": "b" * 40,
                 "evidence": {"validated_sha": None, "review_sha": "a" * 40, "reviewed_sha": None,
                              "review_record": RECORD, "validation_failure": {"sha": "a" * 40},
                              "tests": TESTS, "validation_plan": PLAN}}
        history = evidence.historical_evidence(entry)
        self.assertEqual(history["review"], evidence.review_report(RECORD, "a" * 40, "b" * 40, False))
        self.assertFalse(history["review"]["current"])
        self.assertEqual({k: history[k] for k in ("reason", "at", "head", "base", "validated", "reviewed",
                                                  "verdict", "independent_review_success", "validation_failed",
                                                  "tests")},
                         {"reason": "PR base changed", "at": 12.5, "head": "a" * 40, "base": "b" * 40,
                          "validated": None, "reviewed": "a" * 40, "verdict": "changes_requested",
                          "independent_review_success": False, "validation_failed": True, "tests": TESTS})
        self.assertEqual(history["validation_checks"],
                         [dict(TESTS[0], performed=True), {"command": "make test", "performed": False}])

    def test_historical_evidence_without_review(self):
        entry = {"reason": "pins changed", "at": 1.0, "head": "a" * 40, "base": "b" * 40,
                 "evidence": {"validated_sha": "a" * 40, "reviewed_sha": "a" * 40, "tests": []}}
        history = evidence.historical_evidence(entry)
        self.assertIsNone(history["review"])
        self.assertIsNone(history["verdict"])
        self.assertTrue(history["independent_review_success"])
        self.assertFalse(history["validation_failed"])
        self.assertEqual(history["validation_checks"], [])

    def test_historical_evidence_with_cleared_validation_failure_is_not_a_failure(self):
        # supersede clears the field with validation_failure=None; that is not a recorded failure.
        entry = {"reason": "PR head changed", "at": 2.0, "head": "a" * 40, "base": "b" * 40,
                 "evidence": {"validated_sha": "a" * 40, "validation_failure": None, "tests": TESTS}}
        history = evidence.historical_evidence(entry)
        self.assertFalse(history["validation_failed"])
        self.assertEqual(history["tests"], TESTS)
        self.assertEqual(history["validation_checks"], [dict(TESTS[0], performed=True)])

    def test_rejected_review_uses_saved_report_or_recorded_feedback(self):
        entry = {"sha": "a" * 40, "base": "b" * 40, "feedback": "raw feedback", "findings": [FINDING],
                 "review": {"agent": "claude"}, "tests": TESTS, "validation_plan": PLAN}
        fallback = evidence.rejected_review(entry, False)
        self.assertEqual(fallback, {
            "commit": "a" * 40, "base": "b" * 40, "current": False, "verdict": "changes_requested",
            "summary": "raw feedback", "reviewer": {"agent": "claude"}, "findings": [FINDING],
            "candidate_verdict": "changes_requested", "candidate_findings": [FINDING],
            "candidate_feedback": "raw feedback", "validation_failed": False,
            "validation_checks": [dict(TESTS[0], performed=True), {"command": "make test", "performed": False}]})
        saved = dict(entry, review_report={"verdict": "pass", "summary": "Fine", "findings": []},
                     validation_failed=True)
        report = evidence.rejected_review(saved, True)
        self.assertEqual((report["verdict"], report["summary"], report["findings"], report["current"]),
                         ("pass", "Fine", [], True))
        self.assertEqual((report["candidate_verdict"], report["candidate_findings"], report["candidate_feedback"]),
                         ("changes_requested", [FINDING], "raw feedback"))
        self.assertTrue(report["validation_failed"])


class CiRecordTests(unittest.TestCase):
    def test_observation_binds_head_base_pins_generation_operation_and_time(self):
        observation = evidence.ci_observation(run(), "pending", "reconcile", at=5.0, context={"dirty": ""})
        self.assertEqual(observation, {"sha": "a" * 40, "base": "b" * 40, "operation": "reconcile",
                                       "state": "pending", "companions": PINS, "readiness_changed": False,
                                       "generation": 2, "at": 5.0, "context": {"dirty": ""}})
        self.assertEqual(evidence.ci_observation(run(), "success", "ci", sha="d" * 40)["sha"], "d" * 40)

    def test_any_binding_change_is_a_new_observation(self):
        first = evidence.ci_observation(run(), "success", "ci", at=1.0)
        self.assertTrue(evidence.observation_changed([], first))
        self.assertFalse(evidence.observation_changed([first], dict(first, at=9.0, operation="reconcile")))
        self.assertTrue(evidence.observation_changed([first], dict(first, operation="reconcile"), evidence.RECORD))
        for key, value in (("sha", "d" * 40), ("base", "e" * 40), ("state", "failure"),
                           ("companions", []), ("generation", 3)):
            with self.subTest(key=key):
                self.assertTrue(evidence.observation_changed([first], dict(first, **{key: value})))

    def test_history_is_current_only_for_exact_head_base_generation_and_pins(self):
        checks = [evidence.ci_observation(run(), "failure", "reconcile", at=1.0, context={"dirty": ""}),
                  evidence.ci_observation(run(), "success", "ci", at=2.0)]
        expected = [
            {"operation": "reconcile", "head": "a" * 40, "base": "b" * 40, "state": "failure", "at": 1.0,
             "readiness_changed": False, "companions": PINS, "generation": 2, "context": {"dirty": ""},
             "current": True},
            {"operation": "ci", "head": "a" * 40, "base": "b" * 40, "state": "success", "at": 2.0,
             "readiness_changed": False, "companions": PINS, "generation": 2, "context": None,
             "current": True}]
        history = evidence.ci_history(run(ci_checks=checks))
        self.assertEqual(history, expected)
        self.assertEqual(evidence.current_ci(history), expected[1])
        for changed in ({"sha": "d" * 40}, {"base_sha": "e" * 40}, {"evidence_generation": 3},
                        {"validated_companions": []}):
            with self.subTest(changed=changed):
                moved = evidence.ci_history(run(ci_checks=checks, **changed))
                # Stale observations keep their complete binding evidence; only currency changes.
                self.assertEqual(moved, [dict(c, current=False) for c in expected])
                self.assertIsNone(evidence.current_ci(moved))
        self.assertFalse(any(c["current"] for c in evidence.ci_history(run(ci_checks=checks), current=False)))

    def test_older_check_records_render_with_defaults(self):
        legacy = {"sha": "a" * 40, "base": "b" * 40, "state": "pending", "companions": PINS}
        history = evidence.ci_history(run(ci_checks=[legacy], evidence_generation=0))
        self.assertEqual(history, [{"operation": "checks", "head": "a" * 40, "base": "b" * 40, "state": "pending",
                                    "at": None, "readiness_changed": False, "companions": PINS, "generation": 0,
                                    "context": None, "current": True}])

    def test_history_keeps_extra_recorded_fields(self):
        observation = dict(evidence.ci_observation(run(), "success", "ci", at=3.0), readiness_changed=True,
                           note="kept")
        entry = evidence.ci_history(run(ci_checks=[observation], sha="d" * 40))[0]
        self.assertEqual((entry["note"], entry["readiness_changed"], entry["current"]), ("kept", True, False))
        self.assertNotIn("sha", entry)


if __name__ == "__main__":
    unittest.main()
