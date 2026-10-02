"""`agent-team pr` support."""
from pathlib import Path
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from .agents import FAMILIES
from .process import TeamError
from .state import ACTIVE, RECOVERY, TERMINAL, CoordinatorBusy, issue_fingerprint

PR_MODES = {
    "review": "Review only: report findings. Do not edit, push, merge the base, or start a repair loop.",
    "revise": ("Review and revise: validate and review the existing head first, then fix findings on the "
               "existing PR branch within the revision budget, revalidate, and review the exact result."),
    "findings": ("Revise the operator-supplied findings on the existing PR branch, then validate and "
                 "independently review the exact result."),
}
PR_GRANTS = ("edit", "push", "github")
IDENTITY = ("number", "url", "head_repo", "head_ref", "base_ref", "state", "draft")
REVIEWER = ("agent", "family", "cli_version", "requested_model", "observed_models")
REFUSED_REVISION = "Independent review cannot be established ({}); revision refused. Use review mode"


def pull_number(project, value):
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
    else:
        url = urlsplit(text)
        parts = url.path.strip("/").split("/")
        if (url.scheme != "https" or url.netloc.lower() not in {"github.com", "www.github.com"}
                or len(parts) < 4 or parts[2] != "pull" or not parts[3].isdigit()):
            raise TeamError("Use a PR number or https://github.com/OWNER/REPO/pull/NUMBER")
        if f"{parts[0]}/{parts[1]}".casefold() != project["repo"].casefold():
            raise TeamError(f"PR URL names {parts[0]}/{parts[1]}, not registered repository {project['repo']}")
        number = int(parts[3])
    if number < 1:
        raise TeamError("PR number must be positive")
    return number


def declared_contributors(contributors):
    declared = sorted(set(contributors or []))
    if not declared or not set(declared) <= set(core.PR_CONTRIBUTORS):
        raise TeamError("Declare every contributor: " + ", ".join(core.PR_CONTRIBUTORS))
    return declared


def closure(pr):
    if pr.get("merged"):
        return "merged"
    return None if pr["state"] == "open" else "closed"


def heads(pr):
    return pr["head"]["sha"], pr["base"]["sha"]


def review_report(record, commit, base, current):
    report = record["report"]
    return {"commit": commit, "base": base, "current": current, "verdict": report["verdict"],
            "summary": report["summary"], "findings": report["findings"],
            "reviewer": {k: record.get(k) for k in REVIEWER}, "patch": record.get("patch")}


def historical_evidence(entry):
    evidence = entry["evidence"]
    record = evidence.get("review_record")
    return {"reason": entry["reason"], "at": entry["at"], "head": entry["head"], "base": entry["base"],
            "validated": evidence.get("validated_sha"), "reviewed": evidence.get("review_sha"),
            "verdict": record["report"]["verdict"] if record else None,
            "review": review_report(record, evidence.get("review_sha"), entry["base"], False) if record else None,
            "independent_review_success": bool(evidence.get("reviewed_sha")),
            "validation_failed": "validation_failure" in evidence, "tests": evidence.get("tests", []),
            "validation_checks": core.validation_checks(evidence.get("tests", []), evidence.get("validation_plan"))}


def rejected_review(entry, current):
    report = entry.get("review_report") or {"verdict": "changes_requested", "summary": entry["feedback"],
                                            "findings": entry["findings"]}
    return {"commit": entry["sha"], "base": entry.get("base"), "current": current,
            "verdict": report["verdict"], "summary": report["summary"], "reviewer": entry.get("review"),
            "findings": report["findings"], "candidate_verdict": "changes_requested",
            "candidate_findings": entry["findings"], "candidate_feedback": entry["feedback"],
            "validation_failed": bool(entry.get("validation_failed")),
            "validation_checks": core.validation_checks(entry["tests"], entry.get("validation_plan"))}


def pr_roles(families, reviewer):
    other = {"codex": "claude", "claude": "codex"}
    if len(families) == 1:
        author = {family: agent for agent, family in FAMILIES.items()}[next(iter(families))]
        if reviewer and reviewer != other[author]:
            raise TeamError(f"The {FAMILIES[reviewer]} family contributed; it cannot review independently")
        return author, other[author]
    return (other[reviewer], reviewer) if reviewer else None


def pr_inheritance(project, prior, mode, number):
    limit_of = lambda r: core.revision_limit(project, r)
    exhausted = [r for r in prior if r.get("handoffs") and r["handoffs"][-1]["round"] >= limit_of(r)]
    if mode != "review" and exhausted:
        raise TeamError(f"PR #{number} reached its revision limit in run {exhausted[-1]['id']}; readoption "
                        "keeps that budget. Use review mode or repair outside Agent Team")
    limit = max((limit_of(r) for r in prior), default=project["max_revisions"])
    round_ = max((r["round"] for r in prior), default=0) + (1 if mode == "findings" else 0)
    if mode != "review" and round_ > limit:
        raise TeamError(f"PR #{number} has no revision budget left; use review mode")
    inherited = {"runs": [r["id"] for r in prior],
                 "contributors": sorted({c for r in prior for c in r.get("contributors", [])}
                                        | {f for r in prior for f in core.contributing_families(r)}),
                 "unresolved_trailers": sorted({t for r in prior for t in r.get("unresolved_trailers", []) +
                                                (r.get("adopted_pr") or {}).get("unresolved_trailers", [])})}
    prior_runs = [{"id": r["id"], "stage": r["stage"], "round": r["round"], "limit": limit_of(r),
                   "handoffs": len(r.get("handoffs", [])), "decisions": [d["action"] for d in r.get("decisions", [])],
                   "revision_history": [{k: e.get(k) for k in ("round", "kind", "sha")}
                                        for e in r.get("revision_history", [])]} for r in prior]
    return limit, round_, inherited, prior_runs


class PullRequests:
    def __init__(self, team):
        self.team = team

    def inspect(self, project, cwd, number):
        """Fetch a PR head, even a fork's."""
        git = core.git
        base = git(cwd, "rev-parse", "HEAD")
        git(cwd, "fetch", "--no-tags", f"https://github.com/{project['repo']}.git", f"refs/pull/{number}/head")
        head = git(cwd, "rev-parse", "FETCH_HEAD^{commit}")
        merge_base = git(cwd, "merge-base", base, head)
        trailers = git(cwd, "log", "--format=%(trailers:key=Agent-Family,valueonly)", f"{merge_base}..{head}")
        authors = git(cwd, "log", "--format=%an", f"{merge_base}..{head}")
        families, unresolved = core.split_trailers(trailers)
        return {"head": head, "base": base, "merge_base": merge_base, "base_contained": merge_base == base,
                "trailer_families": families, "unresolved_trailers": unresolved,
                "commit_authors": sorted({line.strip() for line in authors.splitlines() if line.strip()})}

    def fetch(self, project, number, base_ref, expected, fresh, moved, branch=None):
        core.clone_repository(project["repo"], fresh, base_ref, project["timeout"])
        found = self.inspect(project, fresh, number)
        if (found["head"], found["base"]) != tuple(expected):
            raise TeamError(moved)
        if branch:
            core.git(fresh, "checkout", "--detach", found["head"])
            core.git(fresh, "switch", "-c", branch)
        return found

    def adopt(self, name, reference, mode, contributors, grants=(), reviewer=None, findings=(), plan_only=False):
        team = self.team
        project = team.store.project(name)
        number = pull_number(project, reference)
        if mode not in PR_MODES:
            raise TeamError("Select review, revise, or findings")
        declared = declared_contributors(contributors)
        grants = sorted(set(grants))
        findings = [f.strip() for f in findings if f.strip()]
        for refused, message in (
                (not set(grants) <= set(PR_GRANTS),
                 "Adoption accepts only edit, push, and github grants; it never changes PR readiness"),
                (mode == "review" and {"edit", "push"} & set(grants),
                 "Review-only never edits or pushes; omit the edit and push grants"),
                (mode != "review" and "edit" not in grants, "Revision requires --grant edit"),
                ((mode == "findings") != bool(findings),
                 "Supply --finding text with findings mode, and only with findings mode"),
                (reviewer is not None and reviewer not in FAMILIES, "Unknown reviewer agent"),
                (project["paused"], "Project is paused")):
            if refused:
                raise TeamError(message)
        runs = team.store.repository_runs(name)
        owner = next((r for r in runs if r.get("pr") == number and r["stage"] not in TERMINAL), None)
        if owner:
            raise TeamError(f"PR #{number} is already tracked by run {owner['id']}; continue that run instead")
        if any(r["stage"] in ACTIVE or r["stage"] in RECOVERY or r.get("in_flight") for r in runs):
            raise TeamError("Resolve existing work or continue its tracked run")
        pr = team.github.pr(project["repo"], number)
        if closure(pr):
            raise TeamError(f"PR #{number} is merged; adoption refused" if pr.get("merged") else
                            f"PR #{number} is closed; Agent Team never reopens pull requests")
        head_repo = (pr["head"].get("repo") or {}).get("full_name")
        if not head_repo:
            raise TeamError("PR head repository is unavailable; adoption refused")
        if mode != "review" and pr["base"]["ref"] != project["base"]:
            raise TeamError(f"PR targets {pr['base']['ref']}, not registered base {project['base']}; "
                            "Agent Team never retargets PRs. Use review mode")
        with tempfile.TemporaryDirectory(prefix="agent-team-pr-") as temporary:
            found = self.fetch(project, number, pr["base"]["ref"], heads(pr), Path(temporary) / "input",
                               "PR head or base moved during adoption; retry")
        if any(found["head"] in r.get("rejected_shas", []) for r in runs):
            raise TeamError("PR head was rejected by an earlier run; continue that run and its budget")
        prior = [r for r in runs if r.get("pr") == number]
        limit, round_, inherited, prior_runs = pr_inheritance(project, prior, mode, number)
        families = ({c for c in declared + inherited["contributors"] if c in FAMILIES.values()}
                    | set(found["trailer_families"]))
        unresolved = sorted(set(found["unresolved_trailers"]) | set(inherited["unresolved_trailers"]))
        independence = core.assess_independence(set(declared) | set(inherited["contributors"]), families, unresolved)
        if mode != "review" and not independence["established"]:
            raise TeamError(REFUSED_REVISION.format(independence["reason"]))
        roles = pr_roles(families, reviewer)
        access = (team.github.push_access(project, pr) if mode != "review"
                  else {"allowed": False, "reason": "review-only never pushes"})
        followup = ["revision", "validate"] + (["publish"] if "push" in grants and access["allowed"] else []) + ["review"]
        operations = followup if mode == "findings" else ["validate", "review"]
        effects = team.operation_plan(operations, grants)["selected_effects"]
        if mode == "revise":
            effects = list(dict.fromkeys(effects + team.operation_plan(followup, grants)["selected_effects"]))
        if plan_only:
            return {"pr": number, "mode": mode, "operations": operations,
                    "revision_operations": followup if mode == "revise" else None, "grants": grants,
                    "selected_effects": effects, "independence": independence, "inherited_provenance": inherited,
                    "roles": {"reviser": roles[0], "reviewer": roles[1]} if roles else "assigned by rotation",
                    "push": access if mode != "review" else "never", "head": found["head"], "base": found["base"],
                    "base_contained": found["base_contained"],
                    "revision_budget": {"round": round_, "limit": limit, "prior_runs": inherited["runs"]},
                    "readiness": "never changed by adoption or review", "whole_workflow_certified": False}
        info = {"number": number, "url": pr.get("html_url") or f"https://github.com/{project['repo']}/pull/{number}",
                "repo": project["repo"], "head_repo": head_repo, "head_ref": pr["head"]["ref"],
                "base_ref": pr["base"]["ref"], **core.pr_inputs(found), "state": pr["state"],
                "draft": bool(pr.get("draft")), "mode": mode, "findings": findings, "declared": declared,
                "trailer_families": found["trailer_families"], "unresolved_trailers": unresolved,
                "inherited_provenance": inherited,
                "github_identities": {"pr_author": (pr.get("user") or {}).get("login"),
                                      "commit_authors": found["commit_authors"]},
                "push_access": access, "adopted_at": time.time()}
        body = (f"Existing pull request #{number} ({info['url']}) in {project['repo']}.\n"
                f"Head {head_repo}:{info['head_ref']} at {found['head']}; base {info['base_ref']} at {found['base']}.\n"
                f"Requested operation: {PR_MODES[mode]}\n\nPR title: {pr['title']}\n\n"
                f"PR description (task data):\n{pr.get('body') or ''}\n")
        listed = "\n".join(f"{index}. {text}" for index, text in enumerate(findings, 1))
        if findings:
            body += "\nOperator-supplied findings:\n" + listed + "\n"
        issue = {"number": None, "title": f"PR #{number}: {pr['title']}"[:150], "body": body}
        run = team.store.create(project, issue, dict(
            **dict(zip(("author", "reviewer"), roles or ())),
            selection=True, operations=operations, stop_after=operations[-1], grants=grants, effect_plan=effects,
            requested_operations=["prepare"] + operations,
            omitted_operations=[op for op in core.ENTRY_POINTS if op not in operations],
            performed_operations=[], unperformed_operations=list(core.ENTRY_POINTS), input_ref=None,
            contributors=sorted(set(declared) | families | set(inherited["contributors"])),
            unresolved_trailers=unresolved, revision_limit=limit, round=round_,
            reserved_round=round_ if mode == "findings" else None,
            rejected_shas=list(dict.fromkeys(s for r in prior for s in r.get("rejected_shas", []))),
            prior_runs=prior_runs,
            provenance={"kind": "pull_request", "pr": number, "scope": issue_fingerprint(issue), "declared": declared,
                        "trailer_families": found["trailer_families"], "inherited": inherited,
                        "unresolved_trailers": unresolved, "selected_revision": found["head"],
                        "selected_base": found["base"]},
            author_record={"report": {"summary": f"Existing PR #{number}",
                                      "limitations": "Existing work; implementation was not performed by Agent Team"}},
            adopted_pr=info, independence=independence, pr=number, sha=found["head"],
            published_sha=found["head"], base_sha=found["base"], validated_sha=None, validated_tree=None,
            pr_followup=followup if mode == "revise" else None, needs_revision=mode == "findings",
            feedback=("Operator-supplied findings to address; no coordinator review has verified them:\n"
                      + listed) if findings else ""))
        team.store.save(run, branch=f"agent-team/pr-{number}-{run['id']}")
        return run

    def update(self, run_id, contributors):
        team = self.team
        run = team.store.get(run_id)
        project = team.store.project(run["project"])
        info = run.get("adopted_pr")
        if not info:
            raise TeamError("Run does not track an adopted pull request")
        if run.get("pending_swap"):
            return team.finish_journaled(project, run)
        if run["stage"] in TERMINAL:
            raise TeamError(f"Run is {run['stage']}; adopt the PR in a new run if needed")
        if core.recovering(run) or run["stage"] in {"handoff", "repair"} or (
                run["stage"] == "blocked" and run.get("resume_stage") in {"handoff", "repair"}):
            raise TeamError("After the revision limit, record a decision with decide and adopt repairs with adopt")
        if run["stage"] not in {"stale", "stopped", "blocked"}:
            raise TeamError("Update applies to stale, stopped, or blocked runs")
        if team.finalize_rejection(project, run):
            return run
        declared = declared_contributors(contributors)
        pr = team.github.pr(project["repo"], info["number"])
        stage = closure(pr)
        if stage:
            team.adopted_pr_moved(project, run, stage, f"PR was {stage}")
            team.notify(project, run)
            return run
        if pr["base"]["ref"] != info["base_ref"]:
            raise TeamError(f"PR was retargeted to {pr['base']['ref']}; close this run and adopt the PR again")
        if core.head_moved(info, pr):
            raise TeamError("PR head repository or branch changed; close this run and adopt the PR again")
        cwd = team.store.workspace(run)
        if cwd.exists() and not run.get("git_metadata"):
            raise TeamError("Initial preparation was interrupted; finish it with agent-team resume RUN_ID first")
        if cwd.exists():
            core.assert_metadata(cwd, run["git_metadata"])
            if core.git(cwd, "status", "--porcelain"):
                raise TeamError("The checkout has uncommitted work; inspect it before adopting a changed PR "
                                "(previous work retained)")
        fresh = team.store.run_root(run) / f"pr-update-{time.time_ns()}"
        fresh.parent.mkdir(parents=True, exist_ok=True)
        found = self.fetch(project, info["number"], pr["base"]["ref"], heads(pr), fresh,
                           "PR moved during update; retry (previous work retained)", run["branch"])
        if found["head"] == info["head_sha"] and found["base"] == info["base_sha"] and run["stage"] != "blocked":
            raise TeamError("PR head and base are unchanged; nothing to adopt")
        if found["head"] in run.get("rejected_shas", []):
            raise TeamError("PR head is a rejected commit; push a new commit first (previous work retained)")
        families = core.contributing_families(run, declared) | set(found["trailer_families"])
        provenance = team.reassess(run, declared, found["trailer_families"], found["unresolved_trailers"])
        independence, unresolved = provenance["independence"], provenance["unresolved_trailers"]
        if independence["established"] and FAMILIES[run["reviewer"]] in families:
            independence = {"established": False, "reason": "the assigned reviewer's family contributed"}
        if info["mode"] != "review" and not independence["established"]:
            raise TeamError(REFUSED_REVISION.format(independence["reason"]) + " (previous work retained)")
        update = {"at": time.time(), "declared": declared, "trailer_families": found["trailer_families"],
                  "before": {"head": info["head_sha"], "base": info["base_sha"], "candidate": run.get("sha")},
                  "after": {"head": found["head"], "base": found["base"]},
                  "unpushed_local_commit": run.get("sha") if run.get("sha") not in {None, run.get("published_sha")} else None}
        new_info = dict(info, **core.pr_inputs(found), declared=sorted(set(info["declared"]) | set(declared)),
                        trailer_families=sorted(set(info["trailer_families"]) | set(found["trailer_families"])),
                        unresolved_trailers=unresolved, updates=info.get("updates", []) + [update])
        if found["head"] != info["head_sha"]:
            try:
                core.git(fresh, "merge-base", "--is-ancestor", info["head_sha"], found["head"])
                since = info["head_sha"]
            except TeamError:
                since = found["base"]
            new_info = core.contributed(new_info, "external_update", found["head"], declared, fresh,
                                        found["head"], "^" + since)
        common = dict(provenance, adopted_pr=new_info, independence=independence, base_sha=found["base"], sha=found["head"],
                      published_sha=found["head"], pending_push_sha=None, error=None, resume_stage=None,
                      in_flight=False, contributors=sorted(set(run.get("contributors", [])) | set(declared) | families))
        if not cwd.exists():
            shutil.rmtree(fresh)
            team.store.save(run, **common, stage="prepare")
            return run
        preserved =team.store.run_root(run) / f"author-preserved-{time.time_ns()}"
        update["preserved"] = str(preserved)
        pending = (bool(run.get("needs_revision")) and info["mode"] == "findings"
                   and "revision" not in run.get("performed_operations", []))
        retired = core.retire_evidence(run, "Deliberate adoption of changed PR head or base",
                                       before=run.get("evidence_context"))
        changes = dict(common, git_metadata=core.metadata(fresh), **retired, needs_revision=pending, stage="stopped",
                       next_stage="revision" if pending else "validate",
                       partial_result="Changed PR inputs adopted deliberately; select the next operation")
        return team.swap(project, run, fresh, changes, "pr update RUN_ID", True, preserved)

    def report(self, run_id):
        store = self.team.store
        run = store.get(run_id)
        if not run.get("adopted_pr"):
            raise TeamError("Run does not track an adopted pull request")
        try:
            with store.repository_lock(run["project"]):
                return self.summary(store.get(run_id), True)
        except CoordinatorBusy:
            return self.summary(run, False)

    def currency(self, project, run):
        """(True|False|None if unverifiable, reason)."""
        if run.get("in_flight") or run.get("pending_swap"):
            return None, "an operation is in progress or was interrupted"
        try:
            change = self.team.binding_change(project, run)
        except (TeamError, OSError, KeyError) as exc:
            return None, f"verification failed: {exc}"
        return (False, change[1]) if change else (True, None)

    def handoff(self, run, locked):
        info, store = run["adopted_pr"], self.team.store
        sha, published = run.get("sha"), run.get("published_sha")
        cwd = store.workspace(run)
        if not (sha and published and sha != published and cwd.exists() and run.get("validated_sha") == sha):
            return None
        path = store.artifacts(run) / f"pr-{info['number']}-{published[:12]}-{sha[:12]}.patch"
        if locked:
            core.assert_metadata(cwd, run["git_metadata"])
            path.write_text(core.git(cwd, "format-patch", "--stdout", f"{published}..{sha}") + "\n")
        access = run.get("push_access") or info["push_access"]
        reason = run.get("local_handoff_reason")
        if not reason and not access["allowed"]:
            reason = access["reason"]
        elif not reason and "push" not in (run.get("grants") or []):
            reason = "the push grant was not given"
        return {"commit": sha, "builds_on": published, "checkout": str(cwd),
                "patch": str(path) if path.exists() else None,
                "reason": reason or "publication to the PR branch was not selected", "replacement_pr": "not created"}

    def summary(self, run, locked):
        info = run["adopted_pr"]
        project = self.team.store.project(run["project"])
        sha = run.get("sha")
        history = run.get("revision_history", [])
        reason = core.withheld(run)
        retirement = run.get("evidence_retired")
        current = run["stage"] not in {"stale", "closed", "merged"} and not retirement
        currency = {"verified": False, "reason": retirement["reason"] if retirement else f"the run is {run['stage']}"}
        if current:
            verified, why = (self.currency(project, run) if locked
                             else (None, "a coordinator worker holds the repository"))
            currency, current = {"verified": verified, "reason": why}, verified is True
        historical = [historical_evidence(e) for e in run.get("evidence_invalidations", []) if e.get("evidence")]
        latest = next((h for h in reversed(historical) if h["review"]), None)
        review = latest and latest["review"]
        entries = [e for e in history if e["kind"] == "review"]
        reviews = [rejected_review(e, current and e["sha"] == sha and e.get("base") == run["base_sha"]) for e in entries]
        if run.get("review_record") and run.get("review_sha") == sha:
            review = review_report(run["review_record"], sha, run["base_sha"], current)
        elif entries and not (latest and latest["at"] > entries[-1].get("at", 0)):
            review = reviews[-1]
        handoff = self.handoff(run, locked)
        generation, pins = run.get("evidence_generation", 0), run.get("validated_companions") or []
        checks = [{"operation": c.get("operation", "checks"), "head": c["sha"], "base": c["base"],
                   "state": c["state"], "at": c.get("at"), "readiness_changed": c.get("readiness_changed", False),
                   "current": current and c["sha"] == sha and c["base"] == run["base_sha"]
                   and c.get("generation", 0) == generation and (c.get("companions") or []) == pins}
                  for c in run.get("ci_checks", [])]
        latest_check = next((c for c in reversed(checks) if c["current"]), None)
        marked = [c for c in checks if c["readiness_changed"]]
        unpublished = run.get("unpublished_evidence", [])
        plan = core.validation_checks(run.get("tests", []), run.get("validation_plan")) if current else []
        omitted = [c["command"] for c in plan if not c["performed"]]
        limitations = [text for applies, text in (
            (currency["verified"] is None,
             f"Evidence currency was not verified ({currency['reason']}); results are reported as not current."),
            (currency["verified"] is not None and not current, f"Evidence inputs changed ({currency['reason']}); "
             "earlier validation and review results are historical."),
            (run.get("validated_sha") != sha or not current,
             "Configured validation has not passed for the current candidate."),
            (omitted, f"Configured validation commands omitted after an earlier failure: {', '.join(omitted)}."),
            (not info["base_contained"], f"The head does not contain base {info['base_sha']}; the base was not merged."),
            (reason, f"Independent-review success withheld: {reason}."),
            (not checks, "GitHub CI checks were not checked."),
            (checks and not latest_check, "GitHub CI checks were not checked for the current candidate and base; "
             "earlier observations are historical."),
            (latest_check and latest_check["state"] != "success", "GitHub CI checks for the current candidate did "
             f"not pass (state: {(latest_check or {}).get('state')})."),
            (run["stage"] != "ready", f"Agent Team marked the PR ready for {marked[-1]['head']}; that readiness is "
             "not current." if marked else
             "Readiness was not assessed; Agent Team did not change draft or readiness state."),
            (handoff, f"The revision is local only ({(handoff or {}).get('reason')}). Apply the patch or push the "
             "commit yourself; no replacement PR was created."),
            *((True, f"Review evidence for {item['evidence']} was not published: {item['withheld_reason']}.")
              for item in unpublished)) if applies]
        agent = lambda name: f"{name} ({FAMILIES[name]})"
        return {"run": run["id"], "stage": run["stage"], "next_stage": run.get("next_stage"), "error": run.get("error"),
                "pr": {k: info[k] for k in IDENTITY}, "mode": info["mode"], "requested_findings": info["findings"],
                "adopted": {"head": info["head_sha"], "base": info["base_sha"], "merge_base": info["merge_base"],
                            "base_contained": info["base_contained"], "updates": info.get("updates", [])},
                "candidate": sha, "published": run.get("published_sha"),
                "authorship": {"declared": info["declared"], "trailer_families": info["trailer_families"],
                               "unresolved_trailers": info.get("unresolved_trailers", []),
                               "contributing_families": sorted(core.contributing_families(run)),
                               "inherited": info.get("inherited_provenance"), **core.pr_provenance(run),
                               "github_identities": info["github_identities"],
                               "note": "GitHub identities are not treated as model authorship"},
                "roles": {"reviser": agent(run["author"])
                          if info["mode"] != "review" or run["author_record"].get("agent") else None,
                          "reviewer": agent(run["reviewer"])},
                "independence": run.get("independence"), "current_evidence": current, "currency": currency,
                "validation": {"passed_for_candidate": current and run.get("validated_sha") == sha,
                               "results": run.get("tests", []) if current else [], "checks": plan,
                               "companions": pins},
                "review": review, "review_history": reviews,
                "independent_review_success": bool(current and run.get("reviewed_sha") == sha and not reason),
                "historical_evidence": historical, "ci_checks": checks or "not checked", "current_ci": latest_check,
                "push": run.get("push_access") or info["push_access"], "local_handoff": handoff,
                "unpublished_evidence": unpublished,
                **{k: run.get(k, []) for k in ("performed_operations", "omitted_operations", "unperformed_operations")},
                "revision": {"round": run["round"], "limit": core.revision_limit(project, run),
                             "rejected": run.get("rejected_shas", [])},
                "limitations": limitations}


# Last, to break an import cycle.
from . import coordinator as core  # noqa: E402
