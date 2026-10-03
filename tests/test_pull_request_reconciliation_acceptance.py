"""Extended reconciliation acceptance of adopted PRs: movement during CI reads and validation, offline."""
import unittest

from agent_team import coordinator
from agent_team.agents import FAMILIES
from agent_team.coordinator import LOCAL_CHANGE
from agent_team.process import git, TeamError
# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_pull_request_reconciliation as reconciliation
from tests.support_existing_pull_requests import DRIFT
from tests.support_pull_request_reconciliation import CI_MOVES
from tests.support_pull_requests import scenarios

DECLARED = ["github", "edit"]


class ReconciliationAcceptanceTests(reconciliation.ReconciliationFixture):
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

    @scenarios(*((read, local, other) for read in ("checks", "pending", "failure")
                 for local in ("dirty", "local") for other in ("configuration", "pins", "head")))
    def test_local_movement_with_other_movement_during_ci_read_awaits_declarations(self, read, local, other):
        if read == "checks":
            head, run = self.checks_run()
        else:
            head, run = self.readiness_run()
            self.github.check_state = read
        baseline = self.reload(run).get("evidence_context")

        def move():
            self.read_context = self.context(run)
            self.move(other, run)
            self.change(local, run)
        with self.moving(self.github, "ci", move):
            run = self.ticks(run)
        self.assert_declarations_required(run, baseline, local, other)
        check = self.report(run)["ci_checks"][-1]
        self.assertEqual((check["operation"], check["state"], check["current"]),
                         ("checks" if read == "checks" else "ci", "success" if read == "checks" else read, False))
        self.assert_read_binding(check)
        self.assert_no_success()

    @scenarios(*((local, other) for local in ("dirty", "local") for other in ("configuration", "pins")))
    def test_local_movement_with_other_movement_before_checks_awaits_declarations(self, local, other):
        head, run = self.checks_run()
        baseline = self.reload(run).get("evidence_context")
        self.move(other, run)
        self.change(local, run)
        # Retired by the reconciliation before the read; the tick must not adopt the moved checkout as baseline.
        run = self.ticks(run)
        self.assert_declarations_required(run, baseline, local, other)
        self.assertFalse(run.get("ci_checks"))

    @scenarios("checks", "pending", "failure")
    def test_configuration_movement_alone_is_no_contribution(self, read):
        if read == "checks":
            head, run = self.checks_run()
        else:
            head, run = self.readiness_run()
            self.github.check_state = read
        run = self.tick_moving_ci(run, "configuration")
        self.assert_withdrawn(run, head, "configuration")
        self.assertFalse(run.get("pending_contribution"))
        self.assertNotIn(LOCAL_CHANGE, self.invalidation(run)["reason"])

    @scenarios("dirty", "local")
    def test_second_edit_during_checks_needs_fresh_declaration(self, declared):
        head, run = self.stopped_review(grants=["github"])
        self.change(declared, run)
        # The first change is declared; its retired evidence stops that checks selection.
        self.refuses(DRIFT, self.select, run, ["checks"], DECLARED, contributors=["human"])
        run = self.reload(self.select(run, ["checks"], DECLARED))
        baseline, calls = run["evidence_context"], list(self.agents.calls)
        self.assert_fields(run, commit_contributors=["human"], pending_contribution=None)
        self.assertEqual([c["context"] for c in run["contribution_history"]], [baseline])
        if declared == "dirty":
            self.assertIn("local.txt", baseline["dirty"])
        else:
            self.assertNotEqual(baseline["head"], head)

        def edit():
            self.read_context = self.context(run)
            (self.store.workspace(run) / "second.txt").write_text("second edit\n")
        with self.moving(self.github, "ci", edit):
            run = self.ticks(run)
        # The earlier declaration covers only the state it was made for, so the baseline stays.
        self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None, commit_contributors=["human"],
                           evidence_context=baseline)
        self.assert_local_pending(run)
        self.assertIn("second.txt", run["pending_contribution"]["dirty"])
        self.assertNotIn("second.txt", baseline["dirty"])
        # The CI read stays bound to the declared inputs from before it, as history.
        check = self.report(run)["ci_checks"][-1]
        self.assertEqual((check["operation"], check["state"], check["current"]), ("checks", "success", False))
        self.assertEqual((check["context"], self.read_context), (baseline, baseline))
        self.assert_no_success()
        self.refuses("--contributor declarations", self.select, run, ["validate"], ["edit"])
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None, evidence_context=baseline)
        self.assertIn("second.txt", run["pending_contribution"]["dirty"])
        self.assertEqual(len(run["contribution_history"]), 1)
        self.assertEqual(self.agents.calls, calls)
        # Only a fresh declaration attributes the second edit.
        self.select(run, ["validate"], ["edit"], contributors=["human"])
        run = self.reload(run)
        first, second = run["contribution_history"]
        self.assertEqual((first["declared"], first["context"], second["declared"]), (["human"], baseline, ["human"]))
        self.assertIn("second.txt", second["context"]["dirty"])
        self.assert_fields(run, pending_contribution=None, commit_contributors=["human"],
                           attributed_context=second["context"], evidence_context=second["context"])

    @scenarios("success", "failure")
    def test_author_edit_during_validation_awaits_declarations(self, outcome):
        marker = self.root / "fail-validation"
        command = f"test ! -e '{marker}'"
        self.configure(tests=[command])
        head, run = self.stopped_review(grants=["github"])
        if outcome == "failure":
            marker.write_text("")
        run, calls = self.select(run, ["validate"]), list(self.agents.calls)
        # The author checkout is edited while the command runs against the frozen candidate.
        with self.moving(coordinator, "execute", self.capturing("dirty", run), lambda args, *_: args[0] == "/bin/sh"):
            run = self.ticks(run)
        self.assert_fields(run, stage="stopped", next_stage="validate", validated_sha=None, validated_context=None,
                           reviewed_sha=None, review_record=None)
        self.assert_local_pending(run)
        self.assertIn("Inputs changed during validation", self.invalidation(run)["reason"])
        # The baseline is the author state after the candidate commit and before the commands.
        self.assertEqual(run.get("evidence_context"), self.read_context)
        self.assertEqual((self.read_context["head"], self.read_context["dirty"]), (head, ""))
        self.assertIn("local.txt", run["pending_contribution"]["dirty"])
        self.assertEqual((self.workspace_head(run), self.agents.calls), (head, calls))
        # The result is kept as history for the tested commit, never as evidence for the edit.
        historical = self.assert_retired(self.report(run), head, run["base_sha"], LOCAL_CHANGE,
                                         validation_failed=outcome == "failure",
                                         tests=[{"command": command, "exit_code": int(outcome == "failure")}])
        if outcome == "failure":
            self.assertEqual(historical["validation_failure"]["sha"], head)
            self.assertIn(command, historical["validation_failure"]["feedback"])
        else:
            self.assertEqual(historical["validated"], head)
        self.assert_no_success()
        self.refuses("--contributor declarations", self.select, run, ["validate"], ["edit"])
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", validated_sha=None, reviewed_sha=None,
                           evidence_context=self.read_context)
        self.assertTrue(run["pending_contribution"])
        self.assertEqual((self.workspace_head(run), self.agents.calls), (head, calls))
        # Only a declaration attributes the edit, and it is recorded to the declared contributor.
        self.select(run, ["validate"], ["edit"], contributors=["human"])
        run = self.reload(run)
        contribution = run["contribution_history"][-1]
        self.assertEqual(contribution["declared"], ["human"])
        self.assertIn("local.txt", contribution["context"]["dirty"])
        self.assert_fields(run, pending_contribution=None, commit_contributors=["human"],
                           attributed_context=contribution["context"])

    def implemented(self):
        """An ordinary issue run after the normal prepare and implement ticks, about to validate."""
        run = self.reload(self.ticks(self.team.tick("demo")))
        self.assert_fields(run, stage="validate", adopted_pr=None, selection=None, stop_after=None,
                           commit_contributors=[FAMILIES[run["author"]]])
        self.assertEqual(self.roles(), ["implement"])
        return run

    def assert_stopped_before_staging(self, run, baseline, head, kind, calls):
        """The moved checkout is neither staged nor committed; it waits under the earlier attribution."""
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", next_stage="validate", validated_sha=None, reviewed_sha=None,
                           evidence_context=baseline, attributed_context=baseline)
        self.assert_local_pending(run)
        self.assertIn("Inputs changed before validation", self.invalidation(run)["reason"])
        pending = run["pending_contribution"]
        if kind == "dirty":
            self.assertIn("local.txt", pending["dirty"])
            self.assertNotIn("local.txt", baseline["dirty"])
        else:
            self.assertNotEqual(pending["head"], baseline["head"])
        cwd = self.store.workspace(run)
        self.assertEqual((self.workspace_head(run), git(cwd, "diff", "--cached", "--name-only")), (head, ""))
        self.assertEqual(self.agents.calls, calls)
        # No earlier attribution covers the change, so validation is refused without a declaration.
        self.refuses("--contributor declarations", self.team.continue_run, run["id"], ["validate"])
        run = self.reload(run)
        self.assert_fields(run, stage="stopped", next_stage="validate", validated_sha=None, pending_contribution=pending,
                           evidence_context=baseline, attributed_context=baseline, contribution_history=None)
        self.assertEqual((self.workspace_head(run), git(cwd, "diff", "--cached", "--name-only")), (head, ""))
        self.assertEqual(self.agents.calls, calls)
        return run

    def assert_declared_validation(self, run, kind, calls, contributors):
        """A fresh human declaration attributes the change; validation then tests the intact source."""
        pending = run["pending_contribution"]
        self.team.continue_run(run["id"], ["validate"], ["human"])
        run = self.reload(run)
        self.assertEqual([(c["declared"], c["context"]) for c in run["contribution_history"]], [(["human"], pending)])
        self.assert_fields(run, stage="validate", pending_contribution=None, commit_contributors=contributors,
                           attributed_context=pending, evidence_context=pending)
        run = self.reload(self.ticks(run))
        validated, cwd = self.workspace_head(run), self.store.workspace(run)
        self.assert_fields(run, stage="stopped", next_stage="publish", sha=validated, validated_sha=validated,
                           pending_contribution=None, tests=[{"command": "test -f feature.txt", "exit_code": 0}])
        self.assertIn("human", run["contributors"])
        self.assertEqual(self.agents.calls, calls)
        # The human source is in the validated commit, attributed to its human contributor.
        self.assertEqual((git(cwd, "status", "--porcelain"), git(cwd, "show", f"{validated}:feature.txt"),
                          git(cwd, "show", f"{validated}:local.txt")),
                         ("", "feature", "operator edit" if kind == "dirty" else "operator change"))
        message = git(cwd, "log", "-1", "--format=%an%n%B", validated)
        if kind == "dirty":
            for contributor in contributors:
                self.assertIn("Contributor: human" if contributor == "human" else f"Agent-Family: {contributor}",
                              message)
        else:
            self.assertTrue(message.startswith("Human\nLocal change"), message)
        return run

    @scenarios("dirty", "local")
    def test_ordinary_change_after_implementation_awaits_declarations(self, kind):
        run = self.implemented()
        baseline, calls = run["attributed_context"], list(self.agents.calls)
        self.assertEqual(baseline, self.context(run))
        self.assertEqual(baseline["head"], run["base_sha"])
        self.assertIn("feature.txt", baseline["dirty"])
        # An external edit or commit lands after the author finished and before validation stages.
        self.change(kind, run)
        head = self.workspace_head(run)
        self.assertEqual(head == baseline["head"], kind == "dirty")
        self.ticks(run)
        run = self.assert_stopped_before_staging(run, baseline, head, kind, calls)
        self.assertEqual(run["sha"], baseline["head"])
        run = self.assert_declared_validation(run, kind, calls, sorted({FAMILIES[run["author"]], "human"}))
        self.assertEqual(run["sha"] == head, kind == "local")

    @scenarios(*((failure, kind) for failure in ("clone", "timeout") for kind in ("dirty", "local")))
    def test_ordinary_change_while_validation_blocked_awaits_declarations(self, failure, kind):
        run = self.implemented()
        calls = list(self.agents.calls)
        error = "git failed (128): clone refused" if failure == "clone" else "Timed out: /bin/sh (limit 1s)"

        def fail():
            raise TeamError(error)
        # Injected after the candidate commit: the validation clone fails, or a command times out.
        with self.moving(coordinator, "execute", fail,
                         lambda args, *_: "clone" in args if failure == "clone" else args[0] == "/bin/sh"):
            self.ticks(run)
        run = self.reload(run)
        candidate, baseline = self.workspace_head(run), self.context(run)
        # The commit is real, and its exact state is the durable baseline of the blocked run.
        self.assert_fields(run, stage="blocked", resume_stage="validate", error=error, sha=candidate,
                           validated_sha=None, commit_contributors=None, pending_contribution=None,
                           attributed_context=baseline)
        self.assertNotEqual(candidate, run["base_sha"])
        self.assertEqual((baseline["head"], baseline["dirty"]), (candidate, ""))
        self.assertIn(f"Agent-Family: {FAMILIES[run['author']]}",
                      git(self.store.workspace(run), "log", "-1", "--format=%B", candidate))
        self.change(kind, run)
        head = self.workspace_head(run)
        self.assertEqual(head == candidate, kind == "dirty")
        # The resumed boundary rechecks the source against that baseline before staging anything.
        self.assertEqual(self.team.resume(run["id"])["stage"], "validate")
        self.ticks(run)
        run = self.assert_stopped_before_staging(run, baseline, head, kind, calls)
        self.assertEqual(run["sha"], candidate)
        run = self.assert_declared_validation(run, kind, calls, ["human"])
        cwd = self.store.workspace(run)
        self.assertEqual((git(cwd, "rev-parse", f"{run['sha']}^") == candidate, run["sha"] == head),
                         (True, kind == "local"))


if __name__ == "__main__":
    unittest.main()
