"""Offline fixture for ready-state reconciliation tests of adopted PRs; it defines no tests."""
# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_existing_pull_requests as existing
from tests.support_existing_pull_requests import MOVES, REASONS

REVOKED = ("pending", "Evidence inputs changed; readiness invalidated")
NOTICES = {"head": ("pending", "Changed outside coordinator; review invalidated"),
           "identity": ("pending", "PR head changed; review invalidated"),
           "base": ("failure", "Base changed; integration and review need renewal")}
PINS = ("pending", "Companion pins changed; validation and review need renewal")
CI_MOVES = (*MOVES, "pins", "local", "dirty")


class ReconciliationFixture(existing.ExistingPullRequestFixture):
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

    def assert_declarations_required(self, run, baseline, local, other):
        """A local commit or edit that moved with `other` still awaits contributor declarations."""
        self.assert_local_pending(run)
        self.assertIn(REASONS[other], self.invalidation(run)["reason"])
        self.assertEqual(run.get("evidence_context"), baseline)
        pending = run["pending_contribution"]
        if local == "dirty":
            self.assertIn("local.txt", pending["dirty"])
        else:
            self.assertNotEqual(pending["head"], baseline["head"])
        if other == "head":
            self.assertEqual(run["stage"], "stale")
            return
        calls = list(self.agents.calls)
        self.refuses("--contributor declarations", self.select, run, ["validate"], ["edit"])
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None)
        self.assertTrue(run["pending_contribution"])
        self.assertEqual(self.agents.calls, calls)
