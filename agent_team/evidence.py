"""Pure evidence records. Helpers take and return plain dictionaries and import nothing from the
coordinator, so any caller can build or render review, validation and CI evidence the same way."""
import time

REVIEWER = ("agent", "family", "cli_version", "requested_model", "observed_models")
# What CI reported and what it was bound to; a change in any of them is a new observation.
BINDING = ("sha", "base", "state", "companions", "generation")
# Every compared field of a recorded observation, including which operation read it and its effect.
RECORD = BINDING + ("operation", "readiness_changed")


def validation_checks(tests, planned=None):
    """Each planned command with its result, or marked unperformed when an earlier failure stopped the run."""
    planned = planned or [t["command"] for t in tests]
    return [dict(tests[i], performed=True) if i < len(tests) else {"command": c, "performed": False}
            for i, c in enumerate(planned)]


def review_report(record, commit, base, current):
    """A review verdict bound to the exact commit and base it judged. Every report field is kept."""
    report = record["report"]
    return {"commit": commit, "base": base, "current": current, "verdict": report["verdict"],
            "summary": report["summary"], "findings": report["findings"],
            "reviewer": {k: record.get(k) for k in REVIEWER}, "patch": record.get("patch")}


def historical_evidence(entry):
    """Evidence retired by an input change (`reason`, `at`, `head`, `base` and the retired `evidence`).
    It is never current, but stays reportable."""
    evidence = entry["evidence"]
    record = evidence.get("review_record")
    return {"reason": entry["reason"], "at": entry["at"], "head": entry["head"], "base": entry["base"],
            "validated": evidence.get("validated_sha"), "reviewed": evidence.get("review_sha"),
            "verdict": record["report"]["verdict"] if record else None,
            "review": review_report(record, evidence.get("review_sha"), entry["base"], False) if record else None,
            "independent_review_success": bool(evidence.get("reviewed_sha")),
            "validation_failed": bool(evidence.get("validation_failure")), "tests": evidence.get("tests", []),
            "validation_checks": validation_checks(evidence.get("tests", []), evidence.get("validation_plan"))}


def rejected_review(entry, current):
    """A revision-history entry for a rejected candidate. Entries without a saved report fall back to
    the recorded feedback and findings, so older history still renders completely."""
    report = entry.get("review_report") or {"verdict": "changes_requested", "summary": entry["feedback"],
                                            "findings": entry["findings"]}
    return {"commit": entry["sha"], "base": entry.get("base"), "current": current,
            "verdict": report["verdict"], "summary": report["summary"], "reviewer": entry.get("review"),
            "findings": report["findings"], "candidate_verdict": "changes_requested",
            "candidate_findings": entry["findings"], "candidate_feedback": entry["feedback"],
            "validation_failed": bool(entry.get("validation_failed")),
            "validation_checks": validation_checks(entry["tests"], entry.get("validation_plan"))}


def ci_observation(run, state, operation, sha=None, at=None, context=None):
    """One CI read bound to the exact head, base, validated companion pins and evidence generation."""
    return {"sha": sha or run["sha"], "base": run["base_sha"], "operation": operation, "state": state,
            "companions": run.get("validated_companions") or [], "readiness_changed": False,
            "generation": run.get("evidence_generation", 0),
            "at": time.time() if at is None else at, "context": context}


def observation_changed(checks, observation, keys=BINDING):
    """Whether `observation` differs from the latest recorded one in any of `keys`."""
    last = (checks or [{}])[-1]
    return any(last.get(k) != observation[k] for k in keys)


def ci_history(run, current=True):
    """Recorded CI observations, each marked current only for the run's exact head, base, generation
    and pins. Older observations, including transient failures, stay listed as history with every
    recorded field, so stale entries still show the pins, generation and context they were bound to."""
    sha, pins, generation = run.get("sha"), run.get("validated_companions") or [], run.get("evidence_generation", 0)
    return [{**{k: v for k, v in c.items() if k != "sha"},
             "operation": c.get("operation", "checks"), "head": c["sha"], "base": c["base"],
             "state": c["state"], "at": c.get("at"), "readiness_changed": c.get("readiness_changed", False),
             "companions": c.get("companions") or [], "generation": c.get("generation", 0),
             "context": c.get("context"),
             "current": bool(current and c["sha"] == sha and c["base"] == run.get("base_sha")
                             and c.get("generation", 0) == generation and (c.get("companions") or []) == pins)}
            for c in run.get("ci_checks", [])]


def current_ci(history):
    """The latest current observation from `ci_history`, or None."""
    return next((c for c in reversed(history) if c["current"]), None)
