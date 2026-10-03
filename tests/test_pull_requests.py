"""Existing-PR adoption, offline."""
from contextlib import nullcontext
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_team.cli import parser
from agent_team.coordinator import pull_number
from agent_team.process import git, TeamError

# Module imports, so discovery collects neither fixture class nor other suites here.
from tests import support_existing_pull_requests as existing
from tests.support_existing_pull_requests import (
    ALL, CI_FAILED, DELIBERATE, FOLLOWUP, HISTORICAL_CI, INTERRUPTIONS, MOVES, NO_BUDGET, PUSHED, READY, RECHECK,
    REVISION, STALE_READY, unavailable)
from tests.support_pull_requests import COMMIT, FORK, scenarios


class PullRequestTests(existing.ExistingPullRequestFixture):
    @scenarios((False, []), (True, ["github"]), (True, []))
    def test_review_only_reports_findings_and_stops(self, rejected, grants):
        head = self.open_pr(user="claude-bot")
        self.agents.reject, self.agents.summary = rejected, "Feature text is wrong"
        run = self.adopt_pr(number="https://github.com/example/demo/pull/7" if rejected else "7", grants=grants)
        self.assert_fields(run, pr=7, sha=head, base_sha=self.remote_head("main"), issue=None, contributors=["human"])
        self.assert_fields(run["adopted_pr"], head_repo="example/demo", head_ref="feature", base_ref="main",
                           state="open", draft=True, mode="review")
        self.assertEqual(run["adopted_pr"]["github_identities"]["pr_author"], "claude-bot")
        self.assertTrue(run["independence"]["established"])
        run = self.ticks(run, 3)
        self.assertEqual(self.agents.calls, [(run["reviewer"], "review")])
        if grants:
            self.assert_contains(self.github.comments[self.marker(run, head)], "changes requested",
                                 "GitHub CI checks: not checked by this review.", "not a readiness verdict")
            self.assertIn((head, "failure"), self.github.statuses)
        else:
            self.assert_quiet()
        report = self.report(self.assert_stop_boundary(run, head))
        review = report["review"]
        self.assert_review(review, head, True, "changes_requested" if rejected else "pass", agent=run["reviewer"])
        self.assert_fields(report, ci_checks="not checked", local_handoff=None)
        self.assertIsNone(report["roles"]["reviser"])
        self.assertIn("GitHub CI checks were not checked.", report["limitations"])
        self.assert_limited(report, "Readiness was not assessed")
        if not rejected:
            self.assert_reviewed(run, head)
            self.assert_fields(report, currency={"verified": True, "reason": None})
            self.assertTrue(report["independent_review_success"] and report["validation"]["passed_for_candidate"])
            return
        self.assert_awaiting(run, "implement", review_record=None)
        self.assertIn(head, run["rejected_shas"])
        self.assert_fields(review, summary="Feature text is wrong", candidate_verdict="changes_requested",
                           validation_failed=False)
        self.assertEqual(review["findings"][0]["location"], "feature.txt:1")

    def test_adoption_refuses_invalid_requests(self):
        self.open_pr()
        for mode, grants, message in (
                ("review", ["edit"], "Review-only never edits"), ("review", ["push"], "Review-only never edits"),
                ("revise", [], "requires --grant edit"), ("review", ["readiness"], "never changes PR readiness"),
                ("findings", ["edit"], "--finding")):
            with self.subTest(mode=mode, grants=grants):
                self.refuses(message, self.adopt_pr, mode, grants=grants)
        self.refuses("--finding", self.adopt_pr, findings=["Fix it"])
        self.refuses("Declare every contributor", self.adopt_pr, contributors=())
        self.refuses("not registered repository", self.adopt_pr, number="https://github.com/other/demo/pull/7")
        self.assertEqual((pull_number(self.project, "https://github.com/Example/Demo/pull/7/files"), self.store.runs(),
                          self.agents.calls), (7, [], []))

    def test_revise_fast_forwards_existing_branch(self):
        head = self.open_pr(reject=1)
        run = self.writable()
        self.assert_fields(run, operations=RECHECK, pr_followup=FOLLOWUP)
        run = self.ticks(run, 7)
        self.assert_revised(run, head)
        self.assert_fields(run["adopted_pr"], pushed=[run["sha"]])
        self.assertIn(head, run["rejected_shas"])
        self.assertEqual((self.github.creates, self.pull()["body"], self.pull()["draft"]), (0, "Human description", True))
        self.assert_status(run["sha"], "pending", PUSHED)
        self.assert_no_success()
        self.assertIn(self.marker(run, run["sha"], round_=1), self.github.comments)
        report = self.report(run)
        self.assert_review(report["review"], run["sha"], True)
        self.assert_review(report["review_history"][0], head, False, "changes_requested", run["base_sha"], run["reviewer"])

    @scenarios(True, False, None)
    def test_fork_push_or_local_handoff(self, editable):
        head = self.open_pr(8, "fork-feature", head_repo=FORK, maintainer_can_modify=editable)
        self.github.permissions["example/demo"] = editable
        if editable is None:
            self.github.repo = unavailable
        plan = self.adopt_pr("revise", "8", grants=ALL, plan_only=True)
        self.assertEqual((plan["push"]["allowed"], self.store.runs()), (bool(editable), []))
        self.assertIn("maintainer edits" if editable else FORK, plan["push"]["reason"])
        if editable:
            self.assertIn("publish", plan["revision_operations"])
            self.refuses("github", self.adopt_pr, "revise", "8", grants=["edit", "push"])
            return
        self.agents.reject = 1
        run = self.adopt_pr("revise", "8", grants=ALL)
        self.assertEqual(run["adopted_pr"]["head_repo"], FORK)
        self.assertNotIn("publish", run["pr_followup"])
        run = self.ticks(run, 6)
        self.assert_reviewed(run, run["sha"], validated_sha=run["sha"])
        self.assert_untouched(head, 8, "fork-feature")
        handoff = self.report(run)["local_handoff"]
        self.assert_fields(handoff, commit=run["sha"], builds_on=head, replacement_pr="not created")
        self.assertIn(FORK, handoff["reason"])
        self.assertIn("not be verified" if editable is None else "", handoff["reason"])
        self.assertIn("fixed", Path(handoff["patch"]).read_text())
        self.refuses("published PR head", self.select, run, ["ci"], READY)
        self.store.save(run, stage="ci", grants=READY, operations=["ci"], stop_after="ci")
        self.assert_error(self.ticks(run), "blocked", "published PR head")
        self.assert_no_success()
        self.assertTrue(self.pull(8)["draft"])

    @scenarios((None, ["human", "unknown"], "unknown contributors", ["human", "unknown"], []),
               ("anthropic", ["openai"], "both model families", ["anthropic", "openai"], []),
               ("unknown", ["human"], "unknown or unsupported model families", ["human"], ["unknown"]),
               ("gemini", ["human"], "unknown or unsupported model families", ["human"], ["gemini"]))
    def test_unresolved_authorship_withholds_success(self, trailer, declared, refusal, contributors, unresolved):
        head = self.open_pr(families=[trailer], user="openai-codex")
        self.refuses(refusal, self.editing, contributors=declared)
        run = self.adopt_pr(contributors=declared, grants=["github"])
        self.assertEqual(run["contributors"], contributors)
        self.assert_fields(run["adopted_pr"], unresolved_trailers=unresolved,
                           trailer_families=["anthropic"] if trailer == "anthropic" else [])
        self.assert_not_independent(run, refusal)
        run = self.ticks(run, 3)
        self.assert_withheld(run, head, unresolved)
        self.assert_contains(self.github.comments[self.marker(run, head)], "Review (independence not established)",
                             f"Independent-review success withheld: {run['independence']['reason']}")
        self.assert_no_success()
        self.refuses("exact-commit independent review", self.select, run, ["ci"], ["readiness"])

    def test_single_family_independent_roles(self):
        self.open_pr(families=["openai"])
        self.refuses("cannot review independently", self.adopt_pr, reviewer="codex")
        run = self.adopt_pr()
        self.assert_fields(run, author="codex", reviewer="claude", contributors=["human", "openai"])
        self.store.save(run, stage="closed")
        self.open_pr(9, "branch-9", families=["openai", "gemini"])
        run = self.adopt_pr(number="9")
        self.assert_fields(run, author="codex", reviewer="claude")
        self.assertEqual(run["adopted_pr"]["trailer_families"], ["openai"])
        self.assert_not_independent(run)

    def test_duplicate_adoption_refused(self):
        self.open_pr()
        run = self.adopt_pr()
        self.refuses(f"already tracked by run {run['id']}", self.editing)
        self.store.save(run, stage="closed")
        self.refuses("already has a run", self.adopt_pr)
        owned = self.store.create(self.project, self.github.items[0])
        self.store.save(owned, pr=9, stage="stopped")
        self.refuses(f"already tracked by run {owned['id']}", self.adopt_pr, number="9")
        self.assertEqual(len(self.store.runs()), 2)

    def test_readoption_keeps_exhausted_budget(self):
        self.configure(max_revisions=1)
        self.open_pr(reject=2)
        run = self.until_handoff(self.editing())
        self.assert_fields(run, stage="handoff", round=1)
        self.assertEqual(self.team.decide(run["id"], "stop")["stage"], "closed")
        external = self.push_external()
        self.configure(max_revisions=5)
        calls = list(self.agents.calls)
        self.refuses(f"reached its revision limit in run {run['id']}", self.editing)
        self.refuses("reached its revision limit", self.editing_findings)
        self.assertEqual(len(self.store.runs()), 1)
        review = self.adopt_pr()
        self.assert_fields(review, sha=external, round=1, revision_limit=1)
        self.assert_fields(review["prior_runs"][0], id=run["id"], handoffs=1, decisions=["stop"])
        self.assertEqual((set(review["rejected_shas"]), self.agents.calls), (set(run["rejected_shas"]), calls))

    def test_readoption_continues_budget(self):
        self.configure(max_revisions=2)
        self.open_pr(reject=True)
        run = self.reviewed()
        self.assert_fields(run, stage="stopped", round=1)
        self.store.save(run, stage="closed")
        self.push_external()
        self.assertEqual(self.editing(plan_only=True)["revision_budget"],
                         {"round": 1, "limit": 2, "prior_runs": [run["id"]]})
        self.assertEqual(self.editing_findings(plan_only=True)["revision_budget"]["round"], 2)
        self.store.save(run, round=2)
        self.refuses(NO_BUDGET, self.editing_findings)
        self.assert_fields(self.editing(), round=2, revision_limit=2)

    def test_closed_or_other_base_before_mutation(self):
        self.open_pr()
        self.refuses_closed()
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(8, "release-fix", base="release")
        self.refuses("never retargets", self.editing, number="8")
        run = self.adopt_pr(number="8")
        self.assert_reviewed(self.ticks(run, 3), head)
        self.assertEqual((run["adopted_pr"]["base_ref"], self.pull(8)["base"]), ("release", "release"))

    def test_closed_pr_with_deleted_base(self):
        git(self.remote, "branch", "release", "main")
        self.open_pr(8, "release-fix", base="release")
        snapshot = self.pull(8)["frozen_base"]
        git(self.remote, "branch", "-D", "release")
        with self.assertRaises(TeamError):
            self.github.pr(None, 8)
        self.refuses_closed(8, lambda: self.assertEqual(self.github.pr(None, 8)["base"]["sha"], snapshot))

    @scenarios((7, "feature", "main", ["human", "unknown"], "unknown contributors"),
               (8, "mixed", "main", ["openai", "anthropic"], "both model families"),
               (9, "release-fix", "release", ["human"], "never retargets"))
    def test_refused_revision_never_edits(self, number, branch, base, contributors, message):
        git(self.remote, "branch", "release", "main")
        head = self.open_pr(number, branch, base=base, reject=True)
        run = self.reviewed(str(number), contributors=contributors)
        self.assert_awaiting(run, "implement")
        calls = list(self.agents.calls)
        self.refuses_revision(message, run)
        run = self.reload(run)
        self.assertEqual(run["stage"], "stopped")
        self.store.save(run, stage="revision", grants=["edit"], operations=REVISION, stop_after="validate")
        try:
            self.ticks(run)
        except TeamError as error:
            self.assertRegex(str(error), message)
        run = self.reload(run)
        self.assertNotIn(run["stage"], {"validate", "review"})
        self.assertEqual(self.agents.calls, calls)
        self.assertNotIn("implement", self.roles())
        cwd = self.store.workspace(run)
        self.assertEqual((self.head_of(cwd), git(cwd, "status", "--porcelain"), self.remote_head(branch)),
                         (head, "", head))
        self.change("dirty", run)
        self.store.save(run, stage="validate", evidence_context=self.team.evidence_context(self.store.project("demo"), run))
        self.assertRegex(self.tick_raising(run)["error"], message)
        self.assertEqual((self.head_of(cwd), self.remote_head(branch)), (head, head))

    @scenarios("success", "pending")
    def test_readiness_records_ci_states(self, state):
        head, run = self.readiness_run()
        self.github.check_state = state
        ready = state == "success"
        run = self.ticks(run, 1 if ready else 3)
        report = self.report(run)
        check = report["current_ci"]
        self.assert_fields(check, operation="ci", head=head, base=run["base_sha"], state=state)
        self.assertEqual((report["ci_checks"], run["stage"], check["readiness_changed"], self.pull()["draft"]),
                         ([check], "ready" if ready else "ci", ready, not ready))
        if ready:
            self.assertFalse(any("CI checks" in item for item in report["limitations"]))
            return
        self.assertIn(CI_FAILED.format("pending"), report["limitations"])
        self.github.check_state = "failure"
        self.assertNotEqual(self.tick_raising(run)["stage"], "ready")
        self.assertTrue(self.pull()["draft"])
        report = self.report(run)
        self.assertEqual(([c["state"] for c in report["ci_checks"]], report["current_ci"]["head"]),
                         (["pending", "failure"], head))
        self.assertIn(CI_FAILED.format("failure"), report["limitations"])

    @scenarios("base", "configuration", "pins")
    def test_ci_observations_become_historical(self, change):
        if change == "base":
            head, run = self.readiness_run()
            self.github.check_state = "pending"
            self.assertEqual(self.ticks(run)["stage"], "ci")
            self.advance_base()
            self.assertEqual(self.tick_raising(run)["stage"], "stale")
        else:
            head, run = self.stopped_review(grants=["github"])
            run = self.ticks(self.select(run, ["checks"]))
            self.assert_fields(self.report(run)["current_ci"], operation="checks", state="success", head=head)
            if change == "pins":
                self.team.invalidate_pins(self.store.project("demo"), run)
            else:
                self.change(change)
            run = self.revalidate(run)
            self.assert_reviewed(run, head)
            self.assertTrue(self.report(run)["current_evidence"])
        report = self.report(run)
        self.assertIsNone(report["current_ci"])
        self.assert_fields(report["ci_checks"][0], head=head, current=False)
        self.assertIn(HISTORICAL_CI, report["limitations"])

    @scenarios(("comment", "-review-", "head"), ("status", "success", "base"), ("comment", "-ready", "head"),
               ("ci", "", "local"), ("ci", "", "dirty"),
               *(("run", "status", kind) for kind in ("head", "base", "configuration", "local", "dirty")))
    def test_movement_around_readiness_never_ready(self, method, trigger, kind):
        head, run = self.readiness_run()
        if method == "run":
            self.store.save_writing({"status": {"instructions": "Write in Spanish."}})
        key = 1 if method in {"ci", "run"} else 2
        with self.moving(self.agents if method == "run" else self.github, method, lambda: self.change(kind, run),
                         lambda *args: trigger in args[key], after=method in {"comment", "status"}), \
                patch.object(self.github, "comment", wraps=self.github.comment) as comment:
            run = self.ticks(run)
        ready = trigger == "-ready"
        self.assertEqual(self.roles(), ["review"] + ["status"] * (method == "run"))
        self.assert_fields(run, stage="stale" if kind in {"head", "base"} else "stopped", reviewed_sha=None,
                           validated_sha=None)
        self.assertEqual((self.pull()["draft"], run["ci_checks"][-1]["readiness_changed"]), (not ready, ready))
        if ready:
            limitations = self.report(run)["limitations"]
            self.assertIn(STALE_READY.format(head), limitations)
            self.assertFalse(any("did not change draft" in item for item in limitations))
            return
        self.assertNotIn((7, f"{run['id']}-ready"), self.github.comments)
        self.assertEqual(self.invalidation(run)["head"], head)
        if method != "status":
            self.assert_no_success()
        self.assertNotEqual([s for c, s in self.github.statuses if c == head][-1:], ["success"])
        if method in {"ci", "run"}:
            self.assertFalse([c for c in comment.call_args_list if c.args[2].endswith(("-ready", head))])
        if kind in {"local", "dirty"}:
            self.assert_local_pending(run)

    @scenarios(*zip(INTERRUPTIONS, MOVES), *((None, kind) for kind in MOVES))
    def test_moved_inputs_withhold_repair_adoption_writes(self, point, kind):
        run = self.repair_handoff(grants=["github"])
        head, repair = run["sha"], self.commit("feature", "feature", "repair.txt", "repair\n", "Repair")
        if point:
            self.interrupt(point, lambda: self.adopt_repair(run))
            self.change(kind)
            self.adopt_repair(run)
        else:
            with self.writes("status", lambda state: state == "pending", lambda: self.change(kind)):
                self.adopt_repair(run)
        run = self.reload(run)
        self.assertEqual([(i["type"], i["evidence"]) for i in run["unpublished_evidence"]],
                         [("status", repair)] * bool(point) + [("comment", repair)])
        self.assertEqual(([k for _, k in self.github.comments if "-adopt-" in k],
                          (repair, "pending") in self.github.statuses), ([], not point))
        self.assertEqual((run["outbox"], run["adoptions"][-1]["head"], self.workspace_head(run), self.preserved(run)),
                         ([], repair, repair, [head]))
        self.assert_fields(run, stage="stopped" if kind == "configuration" else "stale", validated_sha=None)

    @scenarios(*MOVES, "pins", "local", "worker", "ready")
    def test_pr_show_verifies_binding(self, change):
        if change == "ready":
            head, run = self.readiness_run()
            run = self.ticks(run)
        else:
            head, run = self.stopped_review()
        report = self.report(run)
        self.assertTrue(report["current_evidence"] and report["independent_review_success"])
        moved = self.change("head" if change == "ready" else change, run)
        busy = self.store.repository_lock("demo") if change == "worker" else nullcontext()
        with self.pinned(change), busy:
            report = self.report(run)
        self.assert_historical(report)
        self.assertIsNone(report["current_ci"])
        self.assert_review(report["review"], head, False)
        self.assertIs(report["currency"]["verified"], None if change == "worker" else False)
        self.assert_limited(report, "not verified" if change == "worker" else "historical")
        self.assertEqual(self.reload(run), run)
        if change == "configuration":
            self.refuses("evidence invalidated", self.select, run, ["review"])
            self.assert_fields(self.reload(run), validated_sha=None, reviewed_sha=None)
        elif change == "head":
            run = self.update(run)
            self.assert_fields(run, sha=moved, review_record=None)
            report = self.report(run)
            historical = self.assert_retired(report, head, run["base_sha"], DELIBERATE, reviewed=head)
            self.assert_review(historical["review"], head, False, agent=run["reviewer"])
            self.assert_review(report["review"], head, False)
        elif change == "base":
            old = run["base_sha"]
            self.assertEqual(self.github.pr(None, 7)["base"]["snapshot_sha"], old)
            run = self.ticks(run)
            self.assert_error(run, "stale", "never merged implicitly")
            self.assert_fields(run, reviewed_sha=None, review_record=None)
            self.assert_retired(self.report(run), head, old, "PR base changed")
            self.assertEqual(len(self.agents.calls), 1)
            run = self.update(run)
            self.assert_fields(run, sha=head, base_sha=moved, reviewed_sha=None)
            self.assertEqual((self.remote_head(), run["adopted_pr"]["base_contained"]), (head, False))
            self.assert_limited(self.report(run), "base was not merged")
            run = self.revalidate(run)
            self.assert_reviewed(run, head, base_sha=moved)
            self.assertTrue(self.report(run)["current_evidence"])

    def test_concurrent_head_change_needs_update(self):
        _, run = self.under_review()
        self.assertEqual(run["stage"], "review")
        external = self.push_external()
        run = self.ticks(run)
        self.assert_error(run, "stale", "pr update")
        self.assertEqual(self.agents.calls, [])
        self.refuses("pr update", self.team.refresh, run["id"])
        run = self.update(run)
        self.assert_awaiting(run, sha=external, validated_sha=None)
        update = run["adopted_pr"]["updates"][0]
        self.assertEqual(update["after"]["head"], external)
        self.assertTrue(Path(update["preserved"]).is_dir() and run["evidence_invalidations"])
        self.assert_fields(self.report(run)["authorship"]["subsequent"][-1], kind="external_update",
                           commit=external, declared=["human"], trailer_families=[])
        self.refuses("unchanged", self.update, run)
        self.assert_reviewed(self.revalidate(run), external)
        self.assertEqual(self.remote_head(), external)

    def test_exhausted_fork_repaired_locally(self):
        self.configure(max_revisions=1)
        base, head = self.remote_head("main"), self.open_pr(8, "fork-feature", head_repo=FORK, reject=2)
        run = self.until_handoff(self.adopt_pr("revise", "8", grants=ALL))
        rejected = run["sha"]
        self.assertIn("local repair checkout", run["handoffs"][-1]["text"])
        checkout = Path(self.team.decide(run["id"], "repair")["repair_checkout"]["path"])
        self.assertEqual(self.head_of(checkout), rejected)
        (checkout / "repair.txt").write_text("repair\n")
        git(checkout, "add", ".")
        git(checkout, *COMMIT, "commit", "-m", "Repair")
        repaired = self.head_of(checkout)
        run = self.adopt_repair(run)
        self.assert_awaiting(run, sha=repaired, base_sha=base, published_sha=head, pending_swap=None)
        self.assertTrue(run["adoptions"][-1]["local"] and rejected in run["rejected_shas"])
        self.agents.reject = False
        run = self.revalidate(run)
        self.assert_reviewed(run, repaired)
        self.assertEqual(self.report(run)["local_handoff"]["commit"], repaired)
        self.assert_untouched(head, 8, "fork-feature")

    @scenarios(None, "head", "identity", "retarget")
    def test_exhausted_fork_repair_adopts_moved_base(self, movement):
        self.configure(max_revisions=1)
        old, head = self.remote_head("main"), self.open_pr(8, "fork-feature", head_repo=FORK, reject=2)
        run = self.team.decide(self.until_handoff(self.adopt_pr("revise", "8", grants=ALL))["id"], "repair")
        rejected, checkout = run["sha"], Path(run["repair_checkout"]["path"])
        (checkout / "repair.txt").write_text("repair\n")
        git(checkout, "add", ".")
        git(checkout, *COMMIT, "commit", "-m", "Repair")
        repaired = self.head_of(checkout)
        base = self.advance_base()
        self.refuses("pr update RUN_ID", self.adopt_repair, run)
        (checkout / "draft.txt").write_text("unfinished\n")
        if movement == "head":
            self.push_external("fork-feature")
        elif movement == "identity":
            git(self.remote, "branch", "other-branch", "fork-feature")
            self.pull(8)["branch"] = "other-branch"
        elif movement == "retarget":
            git(self.remote, "branch", "release", "main")
            self.pull(8)["base"] = "release"
        before = self.reload(run)
        if movement:
            self.refuses({"head": "PR head moved", "identity": "head repository or branch changed",
                          "retarget": "never retargets"}[movement], self.update, run)
        else:
            self.update(run)
        after = self.reload(run)
        # The repair checkout, its uncommitted work, the candidate, budget and history are untouched.
        self.assertEqual((self.head_of(checkout), (checkout / "draft.txt").read_text(), self.workspace_head(after)),
                         (repaired, "unfinished\n", rejected))
        kept = ("stage", "sha", "published_sha", "pr", "round", "revision_limit", "handoffs", "decisions",
                "rejected_shas", "repair_checkout", "grants", "contributors", "provenance", "independence",
                "revision_history")
        self.assertEqual({k: after.get(k) for k in kept}, {k: before.get(k) for k in kept})
        if movement:
            self.assertEqual(after, before)
            return
        self.assert_fields(after, base_sha=base, validated_sha=None, reviewed_sha=None)
        self.assert_fields(after["adopted_pr"], number=8, head_repo=FORK, head_ref="fork-feature", head_sha=head,
                           base_sha=base, base_contained=False)
        self.assert_fields(after["adopted_pr"]["updates"][-1], merged=False, base_fast_forward=True,
                           repair_head=repaired, before={"head": head, "base": old, "candidate": rejected},
                           after={"head": head, "base": base})
        invalidation = self.invalidation(after)
        self.assertEqual((invalidation["head"], invalidation["base"]), (rejected, old))
        self.assertIn("base during local repair", invalidation["reason"])
        self.refuses("unchanged", self.update, after)
        self.refuses("Commit or remove", self.adopt_repair, after)
        (checkout / "draft.txt").unlink()
        run = self.adopt_repair(after)
        self.assert_awaiting(run, sha=repaired, base_sha=base, published_sha=head, validated_sha=None,
                             reviewed_sha=None)
        self.assertEqual((run["round"], run["adoptions"][-1]["base_sha"]), (before["round"] + 1, base))
        self.agents.reject = False
        run = self.revalidate(run)
        self.assert_reviewed(run, repaired, base_sha=base)
        # Review received the exact adopted base, while the candidate still does not contain it.
        self.assertIn(f"Base {base}; candidate {repaired}.", self.agents.prompts["review"])
        workspace = self.store.workspace(run)
        self.assertEqual((self.workspace_head(run), git(workspace, "rev-parse", "refs/agent-team/base")),
                         (repaired, base))
        self.assertNotEqual(git(workspace, "merge-base", base, repaired), base)
        report = self.report(run)
        self.assert_limited(report, "base was not merged")
        self.assertEqual(report["local_handoff"]["commit"], repaired)
        self.assert_untouched(head, 8, "fork-feature")

    def test_failed_reconciliation_notification_keeps_ci(self):
        head, run = self.readiness_run()
        self.assertEqual(self.ticks(run)["stage"], "ready")
        self.github.check_state = "failure"
        with self.moving(self.github, "status", unavailable,
                         lambda *args: args[-1] == "CI changed; waiting for checks"):
            run = self.tick_raising(run)
        # The failure was recorded before the notification failed, so it is reported as current.
        self.assert_fields(run, stage="ci")
        self.assertEqual(run["outbox"][0]["description"], "CI changed; waiting for checks")
        report = self.report(run)
        self.assert_fields(report["current_ci"], operation="reconcile", state="failure", head=head,
                           base=run["base_sha"], generation=run.get("evidence_generation", 0))
        self.assertEqual([(c["operation"], c["state"], c["readiness_changed"]) for c in report["ci_checks"]],
                         [("ci", "success", True), ("reconcile", "failure", False)])
        self.assert_contains(report["limitations"], CI_FAILED.format("failure"), STALE_READY.format(head))
        # A later success keeps the transient failure as history.
        self.github.check_state = "success"
        run = self.ticks(run)
        self.assert_fields(run, stage="ready", outbox=[])
        self.assert_status(head, "pending", "CI changed; waiting for checks")
        self.assertEqual(self.github.statuses[-1], (head, "success"))
        report = self.report(run)
        self.assertEqual([(c["operation"], c["state"]) for c in report["ci_checks"]],
                         [("ci", "success"), ("reconcile", "failure"), ("ci", "success")])
        self.assert_fields(report["current_ci"], operation="ci", state="success", head=head)
        self.assertFalse(any("CI checks" in item for item in report["limitations"]))

    def test_cli_parses_existing_pr_operations(self):
        parse = parser().parse_args
        args = parse(["pr", "review", "demo", "7", "--contributor", "unknown"])
        self.assertEqual((args.pr_command, args.pull, args.contributor), ("review", "7", ["unknown"]))
        args = parse(["pr", "findings", "demo", "https://github.com/example/demo/pull/7",
                      "--contributor", "human", "--grant", "edit", "--finding", "Fix the parser"])
        self.assertEqual((args.finding, parse(["pr", "update", "RUN", "--contributor", "human"]).run_id),
                         (["Fix the parser"], "RUN"))
        with self.assertRaises(SystemExit):
            parse(["pr", "review", "demo", "7", "--contributor", "human", "--grant", "readiness"])
        self.assertEqual(parse(["adopt", "RUN", "--contributor", "unknown"]).contributor, ["unknown"])
        self.refuses("Unknown contributor", self.team.select, "demo", ["validate"], [], task="Validate", ref="main",
                     contributors=["unknown"])
        self.assertEqual(self.store.runs(), [])


if __name__ == "__main__":
    unittest.main()
