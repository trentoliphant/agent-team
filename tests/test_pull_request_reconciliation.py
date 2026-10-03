"""Ready-state reconciliation of adopted PRs: retirement before notification, offline."""
import unittest
from unittest.mock import patch

# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_existing_pull_requests as existing
from tests.support_existing_pull_requests import MOVES, unavailable
from tests.support_pull_requests import scenarios

REVOKED = ("pending", "Evidence inputs changed; readiness invalidated")
NOTICES = {"head": ("pending", "Changed outside coordinator; review invalidated"),
           "identity": ("pending", "PR head changed; review invalidated"),
           "base": ("failure", "Base changed; integration and review need renewal")}
PINS = ("pending", "Companion pins changed; validation and review need renewal")


class ReconciliationTests(existing.ExistingPullRequestFixture):
    def ready_run(self):
        head, run = self.readiness_run()
        run = self.ticks(run)
        self.assertEqual(run["stage"], "ready")
        return head, run

    def posted(self):
        return [(state, description) for _, state, description in self.github.status_descriptions]

    def assert_withdrawn(self, run, head, kind):
        self.assert_fields(run, stage="stopped" if kind in {"configuration", "local", "dirty"} else "stale",
                           validated_sha=None, reviewed_sha=None, review_record=None, readiness_status=None)
        self.assertEqual(self.invalidation(run)["head"], head)
        report = self.report(run)
        self.assertFalse(report["current_evidence"] or report["independent_review_success"])
        self.assertIsNone(report["current_ci"])

    @scenarios(*MOVES)
    def test_failed_movement_notification_keeps_retirement(self, kind):
        head, run = self.ready_run()
        self.change(kind, run)
        with patch.object(self.github, "status", side_effect=unavailable):
            run = self.tick_raising(run)
        # Retirement and every notice were saved before the failing write.
        self.assert_withdrawn(run, head, kind)
        expected = [NOTICES[kind]] * (kind in NOTICES) + [REVOKED]
        self.assertEqual([(i["state"], i["description"]) for i in run["outbox"]], expected)
        self.assertTrue(run["notification_pending"])
        run = self.ticks(run)
        self.assertEqual(run["outbox"], [])
        self.assertEqual(self.posted()[-len(expected):], expected)
        self.assert_withdrawn(run, head, kind)
        self.assertNotEqual([s for c, s in self.github.statuses if c == head][-1], "success")

    def test_failed_pin_invalidation_notification_keeps_retirement(self):
        head, run = self.stopped_review(grants=["github"])
        with patch.object(self.github, "status", side_effect=unavailable) as status:
            self.team.invalidate_pins(self.store.project("demo"), run)
            run = self.reload(run)
            self.assertEqual(status.call_count, 0)
            self.assert_fields(run, stage="stopped", next_stage="validate", validated_sha=None, reviewed_sha=None,
                               notification_pending=True)
            self.assertEqual([(i["sha"], i["state"], i["description"]) for i in run["outbox"]], [(head, *PINS)])
            run = self.tick_raising(run)
        # The failed write stays journaled with the retirement, and the next tick retries it.
        self.assert_fields(run, validated_sha=None, reviewed_sha=None)
        self.assertEqual(len(run["outbox"]), 1)
        run = self.ticks(run)
        self.assertEqual(run["outbox"], [])
        self.assert_status(head, *PINS)
        self.assertEqual(self.github.statuses[-1], (head, "pending"))

    def test_read_only_pin_invalidation_queues_nothing(self):
        head, run = self.stopped_review(grants=[])
        self.team.invalidate_pins(self.store.project("demo"), run)
        run = self.ticks(self.reload(run))
        self.assert_fields(run, validated_sha=None, reviewed_sha=None, outbox=[])
        self.assert_quiet()

    @scenarios(("success", "head"), ("failure", "base"), ("pending", "identity"), ("failure", "configuration"),
               ("success", "local"), ("pending", "dirty"))
    def test_movement_during_ready_ci_read_retires_evidence(self, state, kind):
        head, run = self.ready_run()
        self.github.check_state = state
        with self.moving(self.github, "ci", lambda: self.change(kind, run)):
            run = self.ticks(run)
        self.assert_withdrawn(run, head, kind)
        self.assertEqual(run["outbox"], [])
        posted = self.posted()
        self.assertIn(REVOKED, posted)
        self.assertNotIn(("pending", "CI changed; waiting for checks"), posted)
        if kind in NOTICES:
            self.assertIn(NOTICES[kind], posted)
        if kind in {"local", "dirty"}:
            self.assert_local_pending(run)
        # The CI read is kept, but only as history for the retired evidence.
        checks = self.report(run)["ci_checks"]
        self.assertFalse(any(c["current"] for c in checks))
        self.assertEqual([(c["operation"], c["state"]) for c in checks],
                         [("ci", "success")] + [("reconcile", state)] * (state != "success"))


if __name__ == "__main__":
    unittest.main()
