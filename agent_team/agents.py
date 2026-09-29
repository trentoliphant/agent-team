"""Subscription-only adapters for official local CLIs."""
import json
from pathlib import Path
import re

from .process import execute, worker_env, TeamError, QuotaError

FAMILIES = {"codex": "openai", "claude": "anthropic"}
AUTHOR_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"summary": {"type": "string"}, "limitations": {"type": "string"}},
    "required": ["summary", "limitations"],
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "changes_requested"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {k: {"type": "string"} for k in ["severity", "location", "evidence", "request"]},
            "required": ["severity", "location", "evidence", "request"],
        }},
    }, "required": ["verdict", "summary", "findings"],
}
DISCOVERY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"issues": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {k: {"type": "string"} for k in ["title", "evidence", "acceptance"]},
        "required": ["title", "evidence", "acceptance"],
    }}}, "required": ["issues"],
}
STATUS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"message": {"type": "string"}}, "required": ["message"],
}


def validate_report(value, schema):
    """Validate the deliberately small schema subset used by our contracts."""
    kind = schema["type"]
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str)}[kind]
    if not valid:
        raise TeamError(f"Agent report expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise TeamError("Agent report has invalid verdict")
    if kind == "object":
        if set(value) != set(schema["required"]):
            raise TeamError("Agent report has missing or unexpected fields")
        for key, entry in value.items():
            validate_report(entry, schema["properties"][key])
    if kind == "array":
        for entry in value:
            validate_report(entry, schema["items"])


def subscription_status(agent):
    env = worker_env()
    if agent == "codex":
        result = execute(["codex", "login", "status"], env=env, check=False)
        if result.returncode or "logged in using chatgpt" not in (result.stdout + result.stderr).lower():
            raise TeamError("Codex needs a ChatGPT subscription login: run codex login")
    elif agent == "claude":
        result = execute(["claude", "auth", "status", "--json"], env=env, check=False)
        try:
            status = json.loads(result.stdout)
        except ValueError as exc:
            raise TeamError("Cannot verify Claude subscription; run claude auth login") from exc
        if (result.returncode or not status.get("loggedIn") or status.get("authMethod") != "claude.ai"
                or not status.get("subscriptionType") or status.get("apiProvider") != "firstParty"):
            raise TeamError("Claude needs a first-party subscription login: run claude auth login")
    else:
        raise TeamError(f"Unsupported agent: {agent}")
    return execute([agent, "--version"], env=env).stdout.strip()


class Agents:
    def run(self, agent, role, prompt, cwd, artifacts, project):
        version = subscription_status(agent)
        schema = {"implement": AUTHOR_SCHEMA, "review": REVIEW_SCHEMA,
                  "discover": DISCOVERY_SCHEMA, "status": STATUS_SCHEMA}[role]
        artifacts = Path(artifacts)
        artifacts.mkdir(parents=True, exist_ok=True)
        schema_file = artifacts / "schema.json"
        schema_file.write_text(json.dumps(schema))
        output_file = artifacts / "result.json"
        output_file.unlink(missing_ok=True)
        env = worker_env()
        empty_gh = artifacts / "empty-gh"
        empty_gh.mkdir(exist_ok=True)
        env["GH_CONFIG_DIR"] = str(empty_gh)
        model = project.get(f"{agent}_model")
        if agent == "codex":
            args = ["codex", "--no-daemon", "-a", "never", "exec", "--ignore-user-config",
                    "--ignore-rules", "--ephemeral", "--color", "never", "--json",
                    "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
                    "--sandbox", "workspace-write" if role == "implement" else "read-only",
                    "--output-schema", str(schema_file), "-o", str(output_file)]
            if model:
                args += ["--model", model]
            args += ["-"]
        else:
            # --bare deliberately NOT used: it disables subscription authentication.
            toolset = "Read,Glob,Grep,Edit,Write" if role == "implement" else "Read,Glob,Grep"
            args = ["claude", "--safe-mode", "--restricted", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
                    "--no-session-persistence", "--permission-mode", "dontAsk",
                    "--tools", toolset, "--allowedTools", toolset,
                    "--max-turns", "40", "-p", "--output-format", "json",
                    "--json-schema", json.dumps(schema)]
            if model:
                args += ["--model", model]
        (artifacts / "prompt.txt").write_text(prompt)
        result = execute(args, cwd=cwd, env=env, input=prompt,
                         timeout=project["timeout"], check=False)
        (artifacts / "stdout.log").write_text(result.stdout)
        (artifacts / "stderr.log").write_text(result.stderr)
        if agent == "claude":
            try:
                envelope = json.loads(result.stdout)
            except ValueError as exc:
                if result.returncode:
                    self.raise_failure(result.stderr)
                raise TeamError("Claude returned malformed JSON; inspect local logs") from exc
            if result.returncode or envelope.get("is_error"):
                if envelope.get("is_error"):
                    self.raise_failure(json.dumps({k: envelope.get(k) for k in ("subtype", "error", "result")}))
                raise TeamError("Claude run failed; inspect local logs")
            report = envelope.get("structured_output")
            models = list(envelope.get("modelUsage", {}))
        else:
            events = []
            for line in result.stdout.splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            if result.returncode or any(e.get("type") in {"error", "turn.failed"} for e in events):
                errors = [e for e in events if e.get("type") in {"error", "turn.failed"}]
                self.raise_failure(json.dumps(errors) if errors else result.stderr)
                raise TeamError("Codex run failed; inspect local logs")
            if not output_file.exists():
                raise TeamError("Codex produced no final report; inspect local logs")
            try:
                report = json.loads(output_file.read_text())
            except ValueError as exc:
                raise TeamError("Codex returned malformed JSON") from exc
            models = sorted({e["model"] for e in events if isinstance(e.get("model"), str)})
        validate_report(report, schema)
        if role == "review" and report["verdict"] == "pass" and report["findings"]:
            raise TeamError("Review claims pass but contains findings; require an unambiguous verdict")
        record = dict(agent=agent, family=FAMILIES[agent], cli_version=version,
                      requested_model=model or "CLI default", observed_models=models,
                      report=report)
        output_file.write_text(json.dumps(record, indent=2))
        return record

    @staticmethod
    def raise_failure(text):
        if re.search(r"usage limit|rate.?limit|quota (?:exceeded|exhausted)|hit your limit|insufficient_quota",
                     text, re.I):
            raise QuotaError("Subscription capacity unavailable; queued for retry without API fallback")
