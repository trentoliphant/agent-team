"""Ready-state reconciliation of adopted PRs: retirement before notification, offline."""
import unittest
from unittest.mock import patch

# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_existing_pull_requests as existing
from tests.support_existing_pull_requests import MOVES, READY, REASONS, unavailable
from tests.support_pull_requests import scenarios

REVOKED = ("pending", "Evidence inputs changed; readiness invalidated")
NOTICES = {"head": ("pending", "Changed outside coordinator; review invalidated"),
           "identity": ("pending", "PR head changed; review invalidated"),
           "base": ("failure", "Base changed; integration and review need renewal")}
PINS = ("pending", "Companion pins changed; validation and review need renewal")
CI_MOVES = (*MOVES, "pins", "local", "dirty")


class ReconciliationTests(existing.ExistingPullRequestFixture):
    def ready_run(self):
        head, run = self.readiness_run()
        context = self.context(run)
        run = self.ticks(run)
        self.assertEqual(run["stage"], "ready")
        # A successful read is bound to the inputs it was taken with.
        self.assertEqual(self.report(run)["current_ci"]["context"], context)
        return head, run

    def context(self, run):
        return self.team.evidence_context(self.store.project("demo"), self.reload(run))

    def capturing(self, kind, run):
        """Record the inputs at the start of the CI read, then move `kind` during it."""
        def move():
            self.read_context = self.context(run)
            self.move(kind, run)
        return move

    def posted(self):
        return [(state, description) for _, state, description in self.github.status_descriptions]

    def assert_withdrawn(self, run, head, kind):
        self.assert_fields(run, stage="stopped" if kind in {"configuration", "pins", "local", "dirty"} else "stale",
                           validated_sha=None, reviewed_sha=None, review_record=None, readiness_status=None)
        self.assertEqual(self.invalidation(run)["head"], head)
        report = self.report(run)
        self.assertFalse(report["current_evidence"] or report["independent_review_success"])
        self.assertIsNone(report["current_ci"])

    def move(self, kind, run):
        """Change `kind` of input; pins change only from this call on."""
        if kind != "pins":
            return self.change(kind, run)
        pinned = self.pinned("pins")
        pinned.start()
        self.addCleanup(pinned.stop)

    def checks_run(self):
        head, run = self.stopped_review(grants=["github"])
        return head, self.select(run, ["checks"])

    def tick_moving_ci(self, run, kind):
        with self.moving(self.github, "ci", self.capturing(kind, run)):
            return self.ticks(run)

    def assert_read_binding(self, check):
        """The observation keeps the inputs from before the read, never the movement during it."""
        self.assertEqual(check["context"], self.read_context)
        self.assertEqual(check["context"]["dirty"], "")

    def assert_moved_during_ci(self, run, head, kind, operation, state):
        self.assert_withdrawn(run, head, kind)
        self.assertIn({**REASONS, "identity": "repository or branch"}[kind], self.invalidation(run)["reason"])
        self.assertEqual(run["outbox"], [])
        if kind in {"local", "dirty"}:
            self.assert_local_pending(run)
        # The CI read is persisted, but only as history for the retired evidence.
        checks = self.report(run)["ci_checks"]
        self.assertEqual([(c["operation"], c["state"], c["current"]) for c in checks], [(operation, state, False)])
        self.assert_read_binding(checks[0])
        if kind in {"configuration", "local", "dirty"}:
            self.assertNotEqual(checks[0]["context"], self.context(run))
        self.assertTrue(self.pull()["draft"])
        self.assertNotIn(("pending", "CI changed; waiting for checks"), self.posted())
        self.assert_no_success()

    @scenarios(*MOVES)
    def test_failed_movement_notification_keeps_retirement(self, kind):
        head, run = self.ready_run()
        self.change(kind, run)
        with patch.object(self.github, "status", side_effect=unavailable):
            run = self.tick_raising(run)
        # Retirement and every notice were saved before the failing write.
        self.assert_withdrawn(run, head, kind)
        expected = ([NOTICES[kind]] if kind in NOTICES else []) + [REVOKED]
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
        self.assert_fields(run, validated_sha=None, reviewed_sha=None)
        self.assertFalse(run.get("outbox"))
        self.assert_quiet()

    def test_ready_pin_invalidation_retires_readiness(self):
        head, run = self.ready_run()
        # An interrupted earlier readiness change left its intent behind.
        self.store.save(run, readiness_intent=head)
        with patch.object(self.github, "status", side_effect=unavailable):
            self.team.invalidate_pins(self.store.project("demo"), run)
            run = self.reload(run)
            self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None, readiness_intent=None,
                               readiness_status=None, notification_pending=True)
            self.assertEqual([(i["sha"], i["state"], i["description"]) for i in run["outbox"]], [(head, *PINS)])
            run = self.tick_raising(run)
        self.assert_fields(run, validated_sha=None, reviewed_sha=None, readiness_intent=None, readiness_status=None)
        self.assertEqual(len(run["outbox"]), 1)
        run = self.ticks(run)
        self.assertEqual(run["outbox"], [])
        self.assertEqual(self.github.status_descriptions[-1], (head, *PINS))
        # Fresh checks of the same commit on an already nondraft PR do not claim a readiness change.
        run = self.revalidate(run)
        self.assert_reviewed(run, head)
        with patch.object(self.github, "mark_ready", wraps=self.github.mark_ready) as mark_ready:
            run = self.ticks(self.select(run, ["ci"], READY))
        self.assertEqual((run["stage"], mark_ready.call_count, self.pull()["draft"]), ("ready", 0, False))
        check = self.report(run)["current_ci"]
        self.assert_fields(check, operation="ci", head=head, state="success", readiness_changed=False)
        self.assertEqual(self.github.statuses[-1], (head, "success"))

    @scenarios(("success", "head"), ("failure", "base"), ("pending", "identity"), ("failure", "configuration"),
               ("success", "local"), ("pending", "dirty"))
    def test_movement_during_ready_ci_read_retires_evidence(self, state, kind):
        head, run = self.ready_run()
        self.github.check_state = state
        run = self.tick_moving_ci(run, kind)
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
        if state != "success":
            self.assert_read_binding(checks[-1])

    @scenarios(*CI_MOVES)
    def test_movement_during_checks_read_retires_evidence(self, kind):
        head, run = self.checks_run()
        run = self.tick_moving_ci(run, kind)
        self.assert_moved_during_ci(run, head, kind, "checks", "success")

    @scenarios(*((state, kind) for state in ("pending", "failure") for kind in CI_MOVES))
    def test_movement_during_pending_or_failing_ci_read_retires_evidence(self, state, kind):
        head, run = self.readiness_run()
        self.github.check_state = state
        # Movement is rechecked before a pending read returns or a failing read raises.
        run = self.tick_moving_ci(run, kind)
        self.assertNotIn(run["stage"], {"ci", "blocked", "ready"})
        self.assert_moved_during_ci(run, head, kind, "ci", state)

    @scenarios("checks", "pending", "failure")
    def test_dirty_edit_during_ci_read_cannot_enter_validation(self, read):
        if read == "checks":
            head, run = self.checks_run()
        else:
            head, run = self.readiness_run()
            self.github.check_state = read
        baseline, calls = self.reload(run).get("evidence_context"), list(self.agents.calls)
        run = self.tick_moving_ci(run, "dirty")
        # The tick keeps the earlier baseline; the edit waits for contributor declarations.
        self.assertEqual(run.get("evidence_context"), baseline)
        self.assertIn("local.txt", run["pending_contribution"]["dirty"])
        self.assert_read_binding(self.report(run)["ci_checks"][-1])
        self.refuses("--contributor declarations", self.select, run, ["validate"], ["edit"])
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None)
        self.assertTrue(run["pending_contribution"])
        self.assertEqual(self.agents.calls, calls)
        # Only a declaration attributes the edit, and it is recorded to the declared contributor.
        self.select(run, ["validate"], ["edit"], contributors=["human"])
        contribution = self.reload(run)["contribution_history"][-1]
        self.assertEqual(contribution["declared"], ["human"])
        self.assertIn("local.txt", contribution["context"]["dirty"])
        self.assertIsNone(self.reload(run)["pending_contribution"])


if __name__ == "__main__":
    unittest.main()
