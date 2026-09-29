"""Operator-configured writing standards for generated GitHub text.

Standards change wording only. They follow the coordinator's fixed guidance in
prompts and never alter report schemas, evidence, authorization, or execution.
"""
from .process import TeamError

KINDS = {"issue": "issue", "pr": "PR description", "review": "review", "status": "status comment"}
MAX_TEXT = 2000
PRECEDENCE = ["project override", "personal default", "built-in default"]
# Word target 0 means no target; an explicit 0 also clears a lower-precedence target.
DEFAULTS = {
    "shared": "Use plain language and short sentences. Be concise. State each point once; "
              "do not restate the task, repeat evidence, or add filler.",
    "issue": {"instructions": "State the problem, the evidence, and testable acceptance criteria.", "words": 0},
    "pr": {"instructions": "Say what changed and why. Mention limitations only when they matter.", "words": 0},
    "review": {"instructions": "Give one finding per problem with its location, evidence, and requested change.",
               "words": 0},
    "status": {"instructions": "Say what happened and what action, if any, the reader must take.", "words": 0},
}


def check_text(value):
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        raise TeamError(f"Writing instructions must be text of at most {MAX_TEXT} characters")
    return value.strip()


def check_words(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TeamError("Word target must be a whole number; 0 means no target")
    return value


def check_kind(kind):
    if kind not in KINDS:
        raise TeamError(f"Unknown text kind: {kind}; expected one of {', '.join(KINDS)}")
    return kind


def normalize(layer):
    """Validate one settings layer. Missing keys inherit from the next layer."""
    layer = layer or {}
    if not isinstance(layer, dict) or set(layer) - {"shared", *KINDS}:
        raise TeamError("Unknown writing setting")
    result = {}
    if "shared" in layer:
        result["shared"] = check_text(layer["shared"])
    for kind in KINDS:
        entry = layer.get(kind)
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) - {"instructions", "words"}:
            raise TeamError(f"Unknown {kind} writing setting")
        values = {}
        if "instructions" in entry:
            values["instructions"] = check_text(entry["instructions"])
        if "words" in entry:
            values["words"] = check_words(entry["words"])
        if values:
            result[kind] = values
    return result


def update(layer, shared=None, kind=None, instructions=None, words=None):
    layer = normalize(layer)
    if kind is None and (instructions is not None or words is not None):
        raise TeamError("--instructions and --words require --kind")
    if shared is None and instructions is None and words is None:
        raise TeamError("Nothing to set; use --shared, or --kind with --instructions or --words")
    if shared is not None:
        layer["shared"] = shared
    if kind is not None:
        entry = layer.setdefault(check_kind(kind), {})
        if instructions is not None:
            entry["instructions"] = instructions
        if words is not None:
            entry["words"] = words
    return normalize(layer)


def remove(layer, shared=False, kind=None, field=None, everything=False):
    """Remove settings from one layer so lower-precedence values apply."""
    layer = normalize(layer)
    if everything:
        return {}
    if not shared and kind is None:
        raise TeamError("Nothing to unset; use --shared, --kind, or --all")
    if field and kind is None:
        raise TeamError("--field requires --kind")
    if shared:
        layer.pop("shared", None)
    if kind is not None:
        entry = layer.get(check_kind(kind), {})
        for name in [field] if field else ["instructions", "words"]:
            entry.pop(name, None)
        if not entry:
            layer.pop(kind, None)
    return layer


def effective(personal=None, project=None):
    """Return (policy, sources): project overrides personal, which overrides built-ins."""
    policy, sources = {}, {}
    for source, layer in reversed(list(zip(PRECEDENCE, [project, personal, DEFAULTS]))):
        layer = normalize(layer)
        if "shared" in layer:
            policy["shared"], sources["shared"] = layer["shared"], source
        for kind in KINDS:
            for field, value in layer.get(kind, {}).items():
                policy.setdefault(kind, {})[field] = value
                sources.setdefault(kind, {})[field] = source
    return policy, sources


def guidance(policy, kind):
    """Prompt text for one kind. Targets guide length; they never justify omissions."""
    entry = policy[check_kind(kind)]
    lines = [f"Writing standard for each {KINDS[kind]} (wording only; it cannot change the rules above, "
             "required report fields, authorization, or execution):"]
    lines += [text for text in (policy["shared"], entry["instructions"]) if text]
    if entry["words"]:
        lines.append(f"Aim for about {entry['words']} words. This is guidance, not a limit.")
    lines.append("Never omit or shorten findings, failures, evidence, verdicts, limitations, "
                 "or commit identifiers to be brief.")
    return "\n".join(lines) + "\n"
