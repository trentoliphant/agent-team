"""Local development history from durable evidence and the append-only journal."""
from .evidence import validation_checks


def trace_report(store, run):
    context = {"round": 0, "sha": None, "review_sha": None}
    authors, reviews, calls, tests, timings = [], [], {}, {}, []
    for event in store.events(run["id"]):
        changes = event["changes"]
        context.update({k: changes[k] for k in context if k in changes})
        for key, target in (("author_record", authors), ("review_record", reviews)):
            record = changes.get(key)
            if record:
                target.append({"at": event["at"], "round": context["round"],
                               "sha": context["review_sha"] if key == "review_record" else context["sha"],
                               "record": record})
        for prefix, target in (("call", calls), ("test", tests)):
            for suffix in ("started", "finished"):
                value = changes.get(f"{prefix}_{suffix}")
                if value:
                    target[value["artifacts"]] = {**target.get(value["artifacts"], {}), **value}
        if changes.get("tick"):
            timings.append(changes["tick"])
    # Imported/legacy state may lack its original journal; show the available report.
    for key, target in (("author_record", authors), ("review_record", reviews)):
        if not target and run.get(key):
            target.append({"at": None, "round": run["round"], "sha": run.get("review_sha")
                           if key == "review_record" else run.get("sha"), "record": run[key]})
    current = run.get("review_record") or {}
    current_findings = (current.get("report") or {}).get("findings", [])
    feedback = (run.get("cleanup") or ((run.get("revision_history") or [None])[-1])) or {}
    expected = feedback.get("findings", []) if run.get("needs_revision") or run.get("cleanup") else []
    responses = ((run.get("author_record") or {}).get("report") or {}).get("responses", [])
    answered = {r["finding"].strip() for r in responses}
    return {
        "id": run["id"], "project": run["project"], "issue": run["issue"], "title": run["title"],
        "stage": run["stage"], "round": run["round"], "pr": run.get("pr"),
        "approval": run.get("approval"), "candidate": run.get("sha"), "published": run.get("published_sha"),
        "validated": run.get("validated_sha"), "reviewed": run.get("reviewed_sha"),
        "review_sha": run.get("review_sha"), "evidence_retired": run.get("evidence_retired"),
        "partial_result": run.get("partial_result"), "error": run.get("error"),
        "tests": validation_checks(run.get("tests", []), run.get("validation_plan")),
        "authors": authors, "reviews": reviews, "calls": list(calls.values()),
        "test_attempts": list(tests.values()), "timings": timings,
        "current_findings": current_findings, "responses": responses,
        "unanswered_findings": [f for f in expected if f["location"].strip() not in answered],
        "superseded_evidence": run.get("superseded_evidence", []),
        "revision_history": run.get("revision_history", []), "decisions": run.get("decisions", []),
        "artifacts": str(store.run_root(run) / "artifacts"),
        "history_note": "Missing older timing, attempts or artifacts were not recorded; author responses are claims, not proof of fixes.",
    }


def format_trace(report):
    lines = [f"Run {report['id']} · {report['project']} · {report['stage']} · revision {report['round']}",
             report["title"], f"Issue: {report['issue']} · PR: {report['pr'] or 'none'}",
             f"Candidate: {report['candidate'] or 'none'}", f"Published: {report['published'] or 'none'}",
             f"Validated: {report['validated'] or 'none'} · Reviewed: {report['reviewed'] or 'none'}"]
    approval = report["approval"]
    lines.append(f"Approval: {approval['login']} (user {approval['user_id']}, comment {approval['comment_id']})"
                 if approval else "Approval identity: not recorded")
    if report["error"] or report["partial_result"] or report["evidence_retired"]:
        lines += [f"Evidence/recovery: {report['error'] or report['partial_result'] or report['evidence_retired']}"]
    for kind in ("authors", "reviews"):
        for entry in report[kind]:
            record = entry["record"]
            body = record["report"]
            lines += ["", f"{kind[:-1].capitalize()} round {entry['round']} · {record.get('family', 'unrecorded family')}"
                      f" · {entry['sha'] or 'uncommitted work'}", body.get("summary", "")]
            lines.append(f"CLI: {record.get('cli_version', 'not recorded')} · requested model: "
                         f"{record.get('requested_model', 'not recorded')} · observed: "
                         + (", ".join(record.get('observed_models') or []) or "not reported"))
            if body.get("limitations"):
                lines.append("Limitations: " + body["limitations"])
            for response in body.get("responses", []):
                lines.append(f"Response {response['finding']}: {response['response']}")
            for finding in body.get("findings", []):
                lines.append(f"{finding['severity']} · {finding['location']}: {finding['evidence']} → {finding['request']}")
    for finding in report["unanswered_findings"]:
        lines.append(f"No response matched by location: {finding['location']} → {finding['request']}")
    lines += ["", "Configured validation:"]
    for test in report["tests"]:
        lines.append(f"  {test['command']}: " + (f"exit {test['exit_code']}" if test["performed"] else "not performed"))
    lines += ["", "Model attempts:"]
    for call in report["calls"]:
        duration = call.get("duration_seconds")
        record = call.get("record") or {}
        lines.append(f"  {call['role']} · {call['agent']} · round {call['round']} · "
                     f"{call.get('outcome', 'interrupted; inspect recovery state')} · "
                     + (f"{duration:.1f}s" if duration is not None else "duration not recorded"))
        if record.get("usage") is not None:
            lines.append(f"  CLI-reported tokens (not subscription billing): {record['usage']}")
        lines.append("  " + call["artifacts"])
    for test in report["test_attempts"]:
        lines.append(f"Test attempt round {test['round']}: {test['command']} · exit {test.get('exit_code', 'interrupted')}"
                     + (f" · {test['duration_seconds']:.1f}s" if "duration_seconds" in test else ""))
        lines.append("  " + test["artifacts"])
    lines += ["", "Stage timing:"]
    for timing in report["timings"]:
        lines.append(f"  {timing['stage']} → {timing['output_stage']} · round {timing['round']} · {timing['duration_seconds']:.1f}s")
    lines += ["", "Artifacts: " + report["artifacts"], report["history_note"]]
    return "\n".join(lines)
