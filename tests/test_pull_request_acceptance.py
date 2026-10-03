"""Existing-PR adoption acceptance coverage, offline."""
from contextlib import nullcontext, suppress
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_team import coordinator
from agent_team.coordinator import FAMILIES, LOCAL_CHANGE, review_comment
from agent_team.patches import COMPACT_NOTICE
from agent_team.process import git, TeamError

# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_existing_pull_requests as existing
from tests.support_existing_pull_requests import (
    ALL, DELIBERATE, DRIFT, FIX, FOLLOWUP, INTERRUPTIONS, MOVES, NO_BUDGET, PUSHED, REASONS, RECHECK, REVISION,
    STALE_READY, unavailable)
from tests.support_pull_requests import Interrupted, scenarios, trailed


class PullRequestAcceptanceTests(existing.ExistingPullRequestFixture):
    def test_moved_head_never_overwritten(self):
        run = self.at_publish()
        local = run["sha"]
        external = self.push_external()
        run = self.ticks(run)
        self.assertEqual((run["stage"], self.remote_head(), len(self.agents.calls)), ("stale", external, 2))
        run = self.update(run)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual((update["unpushed_local_commit"], run["sha"], self.head_of(Path(update["preserved"]))),
                         (local, external, local))

    def test_interrupted_push_reconciles_without_force(self):
        run = self.at_publish()
        with patch("agent_team.coordinator.git", side_effect=self.lost_push):
            run = self.ticks(run)
        self.assert_fields(run, stage="blocked", pending_push_sha=run["sha"])
        self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_reviewed(run, run["sha"], pending_push_sha=None, outbox=[])
        self.assertEqual((self.remote_head(), run["adopted_pr"]["pushed"]), (run["sha"], [run["sha"]]))
        self.assert_status(run["sha"], "pending", PUSHED)

    @scenarios(None, "comment", "push")
    def test_publishing_local_review(self, crash):
        head = self.open_pr(reject=1)
        run = self.ticks(self.writable(grants=["edit", "github"]), 6)
        sha, key = run["sha"], self.marker(run, run["sha"], 1)
        self.assert_reviewed(run, sha)
        run = self.select(run, ["publish"], ["push", "github"])
        with self.writes("comment", lambda k: crash == "comment" and "-review-" in k, self.crash), \
                patch("agent_team.coordinator.git", side_effect=self.lost_push) if crash == "push" else nullcontext(), \
                self.assertRaises(Interrupted) if crash == "comment" else nullcontext():
            self.ticks(run)
        if crash == "push":
            self.assert_fields(self.reload(run), stage="blocked", pending_push_sha=sha)
            self.assertNotIn(key, self.github.comments)
        if crash and self.ticks(run)["stage"] == "blocked":
            self.team.resume(run["id"])
        run = self.ticks(run)
        self.assert_reviewed(run, sha, published_sha=sha, outbox=[])
        self.assertEqual(self.remote_head(), sha)
        self.assert_status(sha, "pending", "Review reported; readiness not checked")
        self.assert_contains(self.github.comments[key], f"initial PR `{head}`", f"coordinator commit `{sha}`")
        self.assertEqual(self.report(run)["authorship"]["subsequent"][0]["trailer_families"], [FAMILIES[run["author"]]])

    def test_repair_provenance_per_commit(self):
        run = self.repair_handoff(grants=["github"])
        head, family = run["sha"], FAMILIES[run["author"]]
        external = self.push_external(message=trailed("Repair", family))
        self.agents.reject = False
        run = self.revalidate(self.adopt_repair(run))
        self.assert_reviewed(run, external)
        self.assertIn(f"external repair `{external}`: declared human; Agent-Family trailers {family}",
                      self.github.comments[self.marker(run, external, 1)])
        local = self.local_commit(run)
        self.assert_withheld(self.revalidate(run, contributors=["unknown"]), local)
        authorship = self.report(run)["authorship"]
        self.assert_fields(authorship["initial"], commit=head, declared=["human"], trailer_families=[])
        self.assertEqual([(e["kind"], e["commit"], e["declared"], e["trailer_families"], bool(e["commit_authors"]))
                          for e in authorship["subsequent"]], [("external_repair", external, ["human"], [family], True),
                                                               ("local_commit", local, ["unknown"], [], True)])

    @scenarios(True, False)
    def test_permission_lookup_movement_not_pushed(self, moved):
        run = self.at_publish()
        head = self.remote_head()
        with self.moving(self.github, "push_access", lambda: self.local_commit(run)) if moved else \
                patch.object(self.github, "repo", side_effect=unavailable):
            run = self.ticks(run, 2 - moved)
        self.assertEqual((self.remote_head(), run["adopted_pr"].get("pushed", [])), (head, []))
        if moved:
            self.assert_awaiting(run, validated_sha=None, pending_push_sha=None)
            return self.assert_local_pending(run)
        self.assert_reviewed(run, run["sha"], published_sha=head)
        self.assertIn("could not be verified", self.report(run)["local_handoff"]["reason"])

    @scenarios("failed", "intent", "lost")
    def test_readiness_change_needs_confirmation(self, case):
        head, run = self.readiness_run()
        with self.moving(self.github, "mark_ready", unavailable if case == "failed" else self.crash,
                         after=case == "lost"), suppress(Interrupted):
            self.ticks(run)
        run = self.reload(run)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"], run["readiness_intent"]),
                         (case != "lost", False, head))
        self.assert_limited(self.report(run), "ready was not confirmed")
        run = self.ticks(self.team.resume(self.ticks(run)["id"]))
        self.assert_fields(run, stage="ready", readiness_intent=None)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"]), (False, True))

    def test_stale_ready_intent(self):
        run = self.readiness_run()[1]
        self.pull()["draft"] = False
        self.store.save(run, readiness_intent="0" * 40)
        with patch.object(self.github, "mark_ready") as mark:
            run = self.ticks(run)
        self.assert_fields(run, stage="ready", readiness_intent=None)
        self.assertEqual((mark.call_count, run["ci_checks"][-1]["readiness_changed"],
                          self.report(run)["ci_checks"][-1]["readiness_changed"]), (0, False, False))

    @scenarios(*(("review", kind, grants) for kind in ("local", "dirty") for grants in ([], ["github"])),
               ("ready", "local", None), ("ready", "dirty", None))
    def test_local_change_retires_until_declared(self, when, kind, grants):
        if when == "ready":
            head, run = self.readiness_run()
            run = self.ticks(run)
            self.assertEqual(run["stage"], "ready")
            baseline = run["evidence_context"]
            self.change(kind, run)
            run = self.ticks(run, 3)
            self.assertEqual([e["reason"] for e in run["evidence_invalidations"]].count(LOCAL_CHANGE), 1)
            self.assert_status(head, "pending", "Evidence inputs changed; readiness invalidated")
            self.assertIn(STALE_READY.format(head), self.report(run)["limitations"])
        else:
            head, run = self.under_review(grants=grants)
            baseline = run["evidence_context"]
            run = self.tick_moving_during_review(run, lambda: self.change(kind, run))
            self.assertEqual(run["outbox"], [])
            self.assert_quiet()
        self.assert_awaiting(run, reviewed_sha=None, validated_sha=None, evidence_context=baseline)
        self.assertTrue(run["pending_contribution"])
        self.assert_retired(self.report(run), head, run["base_sha"], LOCAL_CHANGE, reviewed=head)
        self.refuses("contributor declarations", self.select, run, RECHECK)
        if kind == "local":
            run = self.revalidate(run, contributors=["human"])
            self.assert_reviewed(run, self.workspace_head(run))
            self.assertNotEqual(run["reviewed_sha"], head)

    @scenarios(("openai", "codex", "claude"), ("anthropic", "claude", "codex"))
    def test_interrupted_adoption_keeps_explicit_roles(self, trailer, author, reviewer):
        if trailer == "openai":
            self.store.save(self.store.create(self.project, self.github.items[0]), stage="closed")
        self.open_pr(families=[trailer])
        real = self.store.save

        def crash(record, **changes):
            if "branch" in changes:
                raise TeamError("Interrupted")
            real(record, **changes)

        with patch.object(self.store, "save", side_effect=crash):
            self.refuses("Interrupted", self.adopt_pr)
        run = next(r for r in self.store.runs() if r.get("adopted_pr"))
        self.assert_fields(run, author=author, reviewer=reviewer)
        self.assertTrue(run["independence"]["established"])

    def test_validation_reports_omitted_commands(self):
        self.configure(tests=["false", "true", "echo skipped"])
        head = self.open_pr()
        run = self.reviewed(grants=["github"])
        self.assert_awaiting(run, "implement")
        report = self.report(run)
        checks = report["validation"]["checks"]
        self.assertEqual([(c["command"], c["performed"], c.get("exit_code")) for c in checks],
                         [("false", True, 1), ("true", False, None), ("echo skipped", False, None)])
        self.assertEqual(report["review"]["validation_checks"], checks)
        self.assert_limited(report, "omitted after an earlier failure: true, echo skipped")
        body = self.github.comments[self.marker(run, head)]
        self.assertEqual(body.count("omitted after an earlier failure"), 2)
        self.assertIn("exit 1", body)

    @scenarios(None, "base", "configuration", "pins")
    def test_queued_review_comment_after_changes(self, change):
        head, run = self.under_review(grants=["github"])
        with patch.object(self.github, "comment", side_effect=unavailable), self.assertRaises(TeamError):
            self.ticks(run)
        run = self.reload(run)
        self.assertEqual((run["stage"], len(run["outbox"])), ("stopped", 1))
        self.store.save(run, readiness_intent=head)
        self.change(change)
        with self.pinned(change):
            run = self.ticks(run)
        self.assertEqual((run["outbox"], len(self.agents.calls)), ([], 1))
        if change is None:
            self.assertIn(self.marker(run, head), self.github.comments)
            return
        self.assertNotIn(self.marker(run, head), self.github.comments)
        self.assertEqual(run["unpublished_evidence"][0]["evidence"], head)
        if change == "base":
            self.assert_error(run, "stale", "PR base changed")
            return
        reason = REASONS[change]
        self.assertEqual((run["stage"], run["next_stage"], self.github.comments, run.get("readiness_intent"),
                          run["unpublished_evidence"][0]["withheld_reason"]), ("stopped", "validate", {}, None, reason))
        report = self.report(run)
        self.assert_review(report["review"], head, False)
        self.assertEqual(self.assert_retired(report, head, run["base_sha"], reason)["reason"], reason)
        self.refuses("validat", self.select, run, ["review"])
        self.assertEqual(len(self.agents.calls), 1)

    @scenarios("head", "local", "handoff", "closed", "merged")
    def test_movement_during_review_retires_evidence(self, case):
        if case == "handoff":
            self.configure(max_revisions=0)
        base, rejected = self.remote_head("main"), case in {"head", "handoff"}
        head, run = self.under_review(rejected, grants=[] if case == "local" else ["github"])
        close = lambda: self.pull().update(state="closed", merged=case == "merged")
        run = self.tick_moving_during_review(run, close if case in {"closed", "merged"} else None)
        report = self.report(run)
        self.assertFalse(report["current_evidence"])
        self.assert_review(report["review"], head, False, "changes_requested" if rejected else "pass")
        self.assertEqual(self.github.comments, {})
        if case == "head":
            self.assert_error(run, "stale", "PR head moved", "before review evidence was published")
            self.assertNotIn((head, "failure"), self.github.statuses)
            self.assertEqual((run["outbox"], {i["evidence"] for i in run["unpublished_evidence"]},
                              len(report["unpublished_evidence"])), ([], {head}, 2))
            self.assert_limited(report, "was not published")
            run = self.ticks(run, 2)
            self.assertEqual((run["stage"], self.github.comments, len(self.agents.calls)), ("stale", {}, 1))
            self.assert_awaiting(self.update(run))
        elif case == "local":
            self.assert_error(run, "stale", "PR head moved")
            self.assert_fields(run, validated_sha=None, reviewed_sha=None)
            self.assertEqual((self.github.statuses, report["validation"]["results"]), ([], []))
            self.assert_limited(report, "historical")
            historical = self.assert_retired(report, head, base, "PR head moved", validated=head,
                                             reviewed=head, independent_review_success=True)
            for review in (historical["review"], report["review"]):
                self.assert_review(review, head, False, base=base, agent=run["reviewer"])
                self.assertTrue(review["summary"])
                self.assertIn("findings", review)
        elif case == "handoff":
            self.assert_fields(run, stage="stale", handoffs=None, round=0, outbox=[])
            self.assertEqual((run.get("rejected_shas", []), self.github.statuses), ([], []))
            self.assertIn(self.marker(run, head)[1], [i.get("marker") for i in run["unpublished_evidence"]])
            self.assertIn("PR head moved", run["evidence_retired"]["reason"])
            self.refuses("handoff", self.team.decide, run["id"], "repair")
        else:
            self.assert_fields(run, stage=case, reviewed_sha=None, outbox=[])
            self.assert_retired(report, head, run["base_sha"], "")
            self.assertEqual(self.pull()["state"], "closed")

    def test_outbox_movement_withholds_later_evidence(self):
        head, run = self.under_review(True, grants=["github"])
        with self.writes("comment", lambda key: "-review-" in key, self.push_external):
            run = self.ticks(run)
        self.assertIn(self.marker(run, head), self.github.comments)
        self.assertNotIn((head, "failure"), self.github.statuses)
        self.assertEqual((run["stage"], run["outbox"], len(self.agents.calls), self.invalidation(run)["head"],
                          [i["type"] for i in run["unpublished_evidence"]]), ("stale", [], 1, head, ["status"]))
        for reason in (run["error"], self.invalidation(run)["reason"]):
            self.assertIn("PR head moved", reason)

    @scenarios("configuration", "local", "dirty", "head", "base")
    def test_moved_rejection_is_history_only(self, kind):
        self.configure(max_revisions=0)
        base = self.remote_head("main")
        head, run = self.under_review(True, grants=["github"])
        run = self.tick_moving_during_review(run, lambda: self.change(kind, run))
        self.assert_fields(run, round=0, handoffs=None, revision_history=None, review_record=None, outbox=[],
                           stage="stale" if kind in {"head", "base"} else "stopped")
        self.assertEqual((run.get("rejected_shas", []), bool(run.get("needs_revision"))), ([], False))
        self.assert_quiet()
        self.assertEqual({i["type"] for i in run["unpublished_evidence"]}, {"comment", "status"})
        self.assert_retired(self.report(run), head, base, REASONS[kind], verdict="changes_requested")
        if kind == "configuration":
            self.agents.reject = False
            self.assert_reviewed(self.revalidate(run), head)

    @scenarios("failed", "crash", "moved")
    def test_post_push_status_survives(self, case):
        run = self.at_publish()
        real_save = self.store.save

        def save(record, **changes):
            real_save(record, **changes)
            if "published_sha" in changes and changes.get("outbox"):
                raise Interrupted()

        with {"failed": self.moving(self.github, "status", unavailable,
                                    lambda *args: args[-1].startswith("Revision pushed")),
              "crash": patch.object(self.store, "save", side_effect=save),
              "moved": self.moving(coordinator, "git", self.push_external, lambda _, *args: "push" in args,
                                   after=True)}[case]:
            try:
                self.ticks(run)
            except (Interrupted, TeamError):
                pass
        run = self.reload(run)
        pushed = run["published_sha"]
        self.assertEqual((self.remote_head() == pushed, run["adopted_pr"]["pushed"]), (case != "moved", [pushed]))
        if case == "moved":
            descriptions = [(s, d) for s, _, d in self.github.status_descriptions if s == pushed]
            self.assertEqual((run["stage"], run["outbox"], descriptions,
                              [i["description"] for i in run["unpublished_evidence"]]), ("stale", [], [], [PUSHED]))
            return
        self.ticks(self.team.resume(run["id"]) if case == "failed" else run)
        if case == "crash":
            self.team.resume(run["id"])
        run = self.ticks(run, 2)
        self.assert_reviewed(run, pushed, outbox=[])
        descriptions = [d for s, _, d in self.github.status_descriptions if s == pushed]
        self.assertIn(PUSHED, descriptions)
        self.assertNotEqual(descriptions[-1], "Coordinator blocked; maintainer action needed")

    @scenarios(*((change, "update") for change in (*MOVES, "local")), ("local", "resume"))
    def test_stale_persisted_rejection_retired(self, change, entry):
        head = self.open_pr(reject=True)
        run = self.ticks(self.editing(), 2)
        with patch.object(self.team, "record_review", side_effect=TeamError("Interrupted")):
            run = self.ticks(run)
        self.assert_fields(run, stage="blocked", review_sha=head)
        self.change(change, run)
        try:
            self.update(run) if entry == "update" else self.ticks(self.team.resume(run["id"]))
        except TeamError:
            pass
        run = self.reload(run)
        self.assert_fields(run, round=0, review_record=None, needs_revision=False, revision_history=None)
        self.assertEqual((run.get("rejected_shas", []), self.roles(),
                          [(h["head"], h["verdict"]) for h in self.report(run)["historical_evidence"] if h["review"]]),
                         ([], ["review"], [(head, "changes_requested")]))
        if change in {"head", "base"}:
            self.assert_awaiting(run)

    def test_review_history_keeps_both_rejections(self):
        self.configure(max_revisions=1, tests=["! grep -q fixed feature.txt"])
        head = self.open_pr(reject=1)
        run = self.until_handoff(self.editing())
        self.assertEqual([e["kind"] for e in run["revision_history"]], ["review", "validation"])
        report = self.report(run)
        for review in (report["review"], *report["review_history"]):
            self.assert_review(review, head, False, "changes_requested", run["base_sha"], run["reviewer"])
            self.assert_fields(review, candidate_verdict="changes_requested", validation_failed=False)

    def test_continuation_and_drift_keep_evidence(self):
        head, run = self.stopped_review()
        local = self.local_commit(run)
        run = self.select(run, RECHECK, contributors=["human"])
        self.assert_review(self.report(run)["review"], head, False, agent=run["reviewer"])
        run = self.ticks(run, 2)
        report = self.report(run)
        historical = report["historical_evidence"][-1]
        self.assert_fields(historical, reason=DRIFT, validated=head, reviewed=head)
        self.assert_review(historical["review"], head, False, base=run["base_sha"], agent=run["reviewer"])
        self.assertTrue(historical["review"]["summary"] and report["independent_review_success"])
        self.assert_review(report["review"], local, True)
        self.store.save(run, stage="closed")
        head = self.open_pr(8, "drift")
        run = self.reviewed("8", count=2)
        (self.store.workspace(run) / "stray.txt").write_text("operator edit\n")
        run = self.ticks(run)
        self.assert_awaiting(run, validated_sha=None)
        self.assert_retired(self.report(run), head, run["base_sha"], DRIFT, verdict=None, validated=head)
        self.assertEqual(self.roles(), ["review", "review"])

    @scenarios("identity", "repository")
    def test_head_identity_change_retires_evidence(self, change):
        head, run = self.stopped_review(grants=["github"])
        self.change(change)
        run = self.ticks(run)
        self.assert_error(run, "stale", "head repository or branch changed")
        self.assert_fields(run, validated_sha=None, reviewed_sha=None)
        report = self.report(run)
        self.assertFalse(report["independent_review_success"])
        self.assertEqual(report["historical_evidence"][-1]["review"]["commit"], head)
        self.assert_status(head, "pending", "PR head changed; review invalidated")
        self.refuses("adopt the PR again", self.update, run)

    @scenarios("repository", "identity")
    def test_repair_refuses_changed_head_identity(self, change):
        run = self.repair_handoff()
        before = self.workspace_head(run)
        self.change(change)
        self.refuses("head repository or branch changed", self.adopt_repair, run)
        after = self.reload(run)
        self.assert_fields(after, stage="repair", adoptions=None, adopted_pr=run["adopted_pr"])
        self.assertEqual((self.workspace_head(after), list(self.store.run_root(after).glob("refresh-*"))), (before, []))

    def test_update_continuation_stops_on_rejection(self):
        head, run = self.stopped_review(self.writable)
        external = self.push_external()
        self.assertEqual(self.ticks(run)["stage"], "stale")
        self.update(run)
        run = self.select(run, RECHECK)
        self.assert_fields(run, pr_followup=None, released_pr_followup=FOLLOWUP)
        self.agents.reject = True
        run = self.ticks(run, 4)
        self.assert_awaiting(run, "implement", sha=external)
        self.assertIn(external, run["rejected_shas"])
        self.assertEqual((self.roles(), self.remote_head(), run["adopted_pr"].get("pushed", [])),
                         (["review", "review"], external, []))

    @scenarios(("revise", ALL), ("review", ["github"]))
    def test_failing_head_is_reviewed_before_any_edit(self, mode, grants):
        head = self.open_pr()
        self.configure(tests=["grep -q fixed feature.txt"])
        run = self.ticks(self.writable(mode, grants), 2)
        self.assert_fields(run, stage="review", validated_sha=None)
        self.assertEqual((run["tests"][0]["exit_code"], run.get("rejected_shas", []), self.agents.calls), (1, [], []))
        run = self.ticks(run)
        entry = run["revision_history"][0]
        self.assert_fields(entry, sha=head, kind="review", validation_failed=True)
        self.assertEqual((self.agents.calls, entry["findings"][0]["severity"], self.remote_head()),
                         ([(run["reviewer"], "review")], "validation", head))
        self.assert_status(head, "failure", "Configured validation failed")
        self.assertIn("exit 1", self.github.comments[self.marker(run, head)])
        if mode == "revise":
            self.assertEqual(run["stage"], "revision")
            run = self.ticks(run, 4)
            self.assert_revised(run, head, validated_sha=run["sha"])
            return
        self.assert_awaiting(run, "implement")
        report = self.report(self.assert_stop_boundary(run, head))
        review = report["review"]
        self.assert_review(review, head, True)
        self.assert_fields(review, summary=self.agents.summary, findings=[], validation_failed=True,
                           candidate_verdict="changes_requested")
        self.assertEqual(review["candidate_findings"][0]["severity"], "validation")
        self.assertFalse(report["validation"]["passed_for_candidate"] or report["independent_review_success"])

    @scenarios(("external_repair", "unknown", ["human"]), ("external_repair", "gemini", ["human"]),
               ("external_repair", None, ["unknown"]), ("local_commit", "gemini", ["human"]))
    def test_unresolved_trailers_withhold_independence(self, kind, value, declared):
        unresolved = [value] if value else []
        if kind == "local_commit":
            head, run = self.stopped_review(self.editing)
            commit = self.local_commit(run, trailed("Local change", value))
            run = self.select(run, RECHECK, contributors=declared)
            self.assert_not_independent(run)
            self.assert_fields(run, unresolved_trailers=unresolved, reviewed_sha=None)
            self.assertEqual(run["adopted_pr"]["unresolved_trailers"], unresolved)
            self.assert_withheld(self.ticks(run, 2), commit)
            self.change("dirty", run)
            self.refuses("Independent review cannot", self.select, run, RECHECK, ALL, contributors=declared)
            git(self.store.workspace(run), "checkout", ".")
            self.assert_error(self.ticks(self.select(run, ["publish"], ["push", "github"])), "blocked", "nothing was pushed")
            self.assertEqual(self.remote_head(), head)
        else:
            run = self.repair_handoff()
            self.assertTrue(run["independence"]["established"])
            commit = self.push_external(message=trailed("Repair", value))
            run = self.adopt_repair(run, declared)
            self.assertEqual(run["sha"], commit)
            self.assert_not_independent(run, value or "unknown contributors")
            self.assertEqual((run["adopted_pr"]["unresolved_trailers"], run["adoptions"][-1]["unresolved_trailers"]),
                             (unresolved, unresolved))
            self.assertEqual(run["contributors"], sorted({"human", *declared}))
            self.refuses_revision("Independent review cannot be established", run)
            self.agents.reject = False
            self.assert_withheld(self.revalidate(run, contributors=declared), commit, unresolved)
            self.assertNotIn("implement", self.roles())
            self.assert_fields(self.report(run)["authorship"]["initial"], declared=["human"], unresolved_trailers=[])
        self.assert_fields(self.report(run)["authorship"]["subsequent"][-1], kind=kind, commit=commit,
                           declared=declared, unresolved_trailers=unresolved)

    @scenarios(*((command, point, closing) for command in ("pr update RUN_ID", "adopt RUN_ID")
                 for point in INTERRUPTIONS for closing in (False, True)))
    def test_interrupted_swap_recovers_inputs(self, command, point, closing):
        updating = command == "pr update RUN_ID"
        run, head, external, recover = self.interrupted_swap(updating, point)
        stored = self.reload(run)
        if closing:
            self.refuses("interrupted", self.command, "close", run["id"])
            self.assertEqual(self.reload(run)["stage"], stored["stage"])
            if point == "rename-2":
                self.store.save(self.reload(run), stage="closed")
            run = recover()
            self.assert_fields(run, pending_swap=None, sha=external, stage="closed" if point == "rename-2" else "stopped")
            self.command("close", run["id"])
            return self.assert_fields(self.reload(run), stage="closed", pending_swap=None)
        self.assert_fields(stored, stage="stale" if updating else "repair", sha=head)
        self.assertEqual(stored["pending_swap"]["command"], command)
        if updating:
            self.refuses("interrupted", self.select, run, ["validate"])
            self.refuses("interrupted", self.team.resume, run["id"])
        else:
            self.refuses("interrupted", self.team.decide, run["id"], "stop")
            later = self.commit("feature", "feature", "later.txt", "later\n", "Later change")
        run = recover()
        self.assert_awaiting(run, pending_swap=None, sha=external, validated_sha=None, reviewed_sha=None)
        self.assertEqual((self.workspace_head(run), run["adopted_pr"]["head_sha"], self.preserved(run), self.remote_head()),
                         (external, external, [head], external if updating else later))
        if updating:
            self.assertEqual((run["evidence_context"]["head"], self.invalidation(run)["reason"]),
                             (external, DELIBERATE))
            self.assert_reviewed(self.revalidate(run), external)
            return
        self.assertEqual(([(a["head"], a["declared"]) for a in run["adoptions"]], run["round"]),
                         ([(external, ["human"])], 1))
        self.assert_error(self.ticks(run), "stale", "agent-team adopt")

    def test_supplied_findings_get_fresh_evidence(self):
        head = self.open_pr()
        run = self.writable("findings", findings=FIX)
        self.assertEqual(run["operations"], FOLLOWUP)
        run = self.ticks(run, 5)
        self.assertEqual((run["stage"], run.get("rejected_shas", [])), ("stopped", []))
        self.assert_contains(self.agents.prompts["implement"], FIX[0], "existing PR #7")
        self.assertNotEqual(run["sha"], head)
        self.assertEqual((run["validated_sha"], run["reviewed_sha"], self.remote_head()), (run["sha"],) * 3)
        self.assertTrue(self.pull()["draft"])

    def test_findings_first_edit_uses_budget(self):
        self.configure(max_revisions=0)
        self.open_pr()
        self.refuses(NO_BUDGET, self.editing_findings)
        self.assertEqual((self.store.runs(), self.agents.calls), ([], []))
        self.configure(max_revisions=1)
        self.agents.reject = True
        run = self.editing_findings()
        self.assert_fields(run, round=1, revision_limit=1)
        run = self.until_handoff(run)
        self.assertEqual((run["stage"], run["round"], self.roles().count("implement")), ("handoff", 1, 1))
        self.store.save(run, stage="stopped", next_stage="revision", round=2, needs_revision=True)
        self.refuses_revision(NO_BUDGET, run)
        self.assertEqual(self.roles().count("implement"), 1)

    def test_exhausted_repair_adopted_without_merge(self):
        run = self.repair_handoff()
        external = self.push_external()
        self.advance_base()
        run = self.adopt_repair(run)
        self.assert_awaiting(run, sha=external)
        self.assert_fields(run["adopted_pr"], head_sha=external, base_contained=False)
        self.assertEqual(self.remote_head(), external)

    @scenarios(0, 2)
    def test_author_entry_needs_reserved_round(self, limit):
        self.configure(max_revisions=limit)
        head, run = self.stopped_review(self.writable)
        self.assertEqual(run["round"], 0)
        for operations in (["implement", *RECHECK], REVISION):
            self.refuses("No revision round is reserved", self.select, run, operations, ["edit"])
        self.store.save(run, round=limit + 1, reserved_round=limit + 1, needs_revision=True)
        self.refuses(NO_BUDGET, self.select, run, ["implement", "validate"], ["edit"])
        self.assertEqual((self.roles(), self.remote_head()), (["review"], head))

    def test_findings_stop_on_unrelated_findings(self):
        self.configure(max_revisions=3)
        self.open_pr(reject=1)
        self.agents.findings = [[{"severity": "P2", "location": "other.txt:3", "evidence": "Unrelated",
                                  "request": "Rewrite other.txt"}]]
        run = self.writable("findings", findings=FIX)
        self.assertIsNone(run["pr_followup"])
        run = self.ticks(run, 6)
        self.assert_awaiting(run, "implement", round=2)
        self.assertEqual(self.roles(), ["implement", "review"])
        self.assertIn("supplied findings remain the revision scope", run["partial_result"])
        report = self.report(run)
        self.assertEqual((report["requested_findings"], report["review"]["findings"][0]["location"]),
                         (FIX, "other.txt:3"))
        self.store.save(run, round=1, reserved_round=1)
        self.refuses_revision("each round allows one pass", run)
        self.assertEqual(len(self.agents.calls), 2)

    @scenarios((["openai"], ["human"], "cannot review independently"),
               (["human", "unknown"], ["human"], "unknown contributors"),
               (["openai"], ["anthropic"], "both model families"))
    def test_readoption_carries_provenance(self, first, second, message):
        self.open_pr()
        run = self.reviewed(contributors=first)
        self.store.save(run, stage="closed")
        self.push_external()
        self.refuses(message, self.editing, contributors=second, reviewer="codex")
        again = self.adopt_pr(contributors=second)
        self.assertEqual(again["adopted_pr"]["inherited_provenance"]["runs"], [run["id"]])
        self.assertTrue(set(first) <= set(again["contributors"]))
        if first == ["openai"] and second == ["human"]:
            self.assert_fields(again, author="codex", reviewer="claude")
        else:
            self.assertIn(message, again["independence"]["reason"])

    @scenarios(True, False)
    def test_large_pr_needs_complete_patch(self, compact_fits):
        lines = [f"line {n}" for n in range(300)]
        self.commit("main", "main", "big.txt", "\n".join(lines) + "\n", "Add big file")
        self.open_pr()
        edited = [f"edited {n}" if n % 50 == 0 else line for n, line in enumerate(lines)]
        head = self.commit("feature", "feature", "big.txt", "\n".join(edited) + "\n", "Edit big file")
        span = f"{self.remote_head('main')}...{head}"
        default, compact = (len(git(self.remote, "diff", *options, span)) + 1 for options in ([], ["--unified=0"]))
        with patch("agent_team.patches.REVIEW_BUDGET", compact if compact_fits else compact - 1):
            run = self.tick_raising(self.adopt_pr(), 3)
        if not compact_fits:
            self.assert_error(run, "blocked", "exceeds review budget even without context")
            self.assert_fields(run, reviewed_sha=None, review_record=None)
            self.assertEqual(self.agents.calls, [])
            return
        self.assert_reviewed(run, head)
        self.assert_fields(run["review_record"]["patch"], format="compact", complete=True, files=2,
                           changed_lines=13, characters=compact, default_characters=default, range=span)
        prompt = self.agents.prompts["review"]
        self.assert_contains(prompt, COMPACT_NOTICE, "-line 50\n+edited 50\n")
        self.assertNotIn("\n line 51\n", prompt)
        self.assertEqual(self.report(run)["review"]["patch"], run["review_record"]["patch"])
        self.assertIn("complete context-free patch", review_comment(head, run["review_record"]))

    def test_interrupted_ready_success_revoked(self):
        head, run = self.readiness_run()
        with self.writes("status", lambda state: state == "success", self.crash), self.assertRaises(Interrupted):
            self.ticks(run)
        self.assertEqual(self.github.statuses[-1], (head, "success"))
        self.advance_base()
        self.assert_fields(self.ticks(run), stage="stale", readiness_status=None)
        self.assertEqual(self.github.statuses[-1], (head, "pending"))

    @scenarios("rename-1", "metadata", "tampered")
    def test_interrupted_preparation_recovers(self, point):
        head = self.open_pr()
        run = self.adopt_pr()
        self.interrupt("metadata" if point == "metadata" else "rename-1", lambda: self.ticks(run))
        if point == "tampered":
            self.local_commit(run)
        self.ticks(run)
        self.refuses("Initial preparation was interrupted", self.update, run)
        run = self.ticks(self.team.resume(run["id"]), 3)
        if point == "tampered":
            self.assertIn("differs from the adopted PR head", run["error"])
            return
        self.assert_reviewed(run, head, installing=None)
        self.assertEqual(self.roles(), ["review"])


if __name__ == "__main__":
    unittest.main()
