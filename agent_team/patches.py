"""The complete patch an independent reviewer receives, within the review budget.

The default diff is used when it fits. Otherwise a context-free diff (`--unified=0`) is used only
after it is proven to have exactly the same file headers and changed lines as the default diff.
Nothing is ever truncated, sliced, or excluded; anything that cannot be proven is refused."""
import hashlib
from pathlib import Path
import re
import tempfile

from .process import TeamError

REVIEW_BUDGET = 180000
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
COMPACT_NOTICE = ("The full diff exceeds the review budget, so this patch has no context lines "
                  "(git diff --unified=0). It is complete: the coordinator verified it has the same files, "
                  "file metadata, and changed lines as the full diff. It does not include surrounding context; "
                  "inspect the full source of the candidate in your working directory.\n")


class Unproven(TeamError):
    """A patch whose structure, and so its equivalence, cannot be established."""


def review_patch(git, cwd, base, sha, limit=None, separator="..."):
    """The patch text for `base...sha` and evidence of its format and completeness.
    `git` is the caller's Git runner, so callers keep their own patch point. `separator` selects
    the range: `...` compares from the merge base, `..` compares `base` itself."""
    limit = REVIEW_BUDGET if limit is None else limit
    span = f"{base}{separator}{sha}"
    default = diff(git, cwd, span)
    text = default.decode("utf-8", errors="replace")
    evidence = {"range": span, "format": "default", "context": "git default", "complete": True,
                "characters": len(text), "default_characters": len(text),
                "sha256": hashlib.sha256(default).hexdigest()}
    if len(text) <= limit:
        return text, evidence
    compact = diff(git, cwd, span, "--unified=0")
    short = compact.decode("utf-8", errors="replace")
    if len(short) > limit:
        raise TeamError(f"Diff exceeds review budget even without context ({len(short)} of {limit} characters); "
                        "split the PR")
    try:
        files = changes(default)
        same = files == changes(compact)
    except Unproven as exc:
        raise TeamError(f"Context-free patch could not be proven complete ({exc}); review refused") from exc
    if not same:
        raise TeamError("Context-free patch differs from the full diff; review refused")
    return short, dict(evidence, format="compact", context="none (--unified=0)", characters=len(short),
                       sha256=hashlib.sha256(compact).hexdigest(), files=len(files),
                       changed_lines=sum(1 for _, lines in files for c in lines if c[0] != "\\"),
                       equivalence="same file headers and changed lines as the default diff")


def diff(git, cwd, span, *options):
    """Raw diff bytes. Written to a file outside the checkout so no output is stripped or decoded."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "patch"
        git(cwd, "diff", "--no-ext-diff", "--no-color", *options, f"--output={out}", span)
        return out.read_bytes()


def changes(raw):
    """Each file's exact header lines and its changed lines with their line numbers, independent
    of context and hunk layout. Hunk lengths come from hunk headers, so source lines that resemble
    diff syntax (`--- a`, `+++ b`, `@@`, `diff --git`, `\\`) remain content."""
    if raw and not raw.endswith(b"\n"):
        raise Unproven("patch does not end with a newline")
    lines, files, i = raw.split(b"\n")[:-1], [], 0
    while i < len(lines):
        if not lines[i].startswith(b"diff --git "):
            raise Unproven(f"line {i + 1} is outside any file")
        header, i = [lines[i]], i + 1
        while i < len(lines) and not lines[i].startswith((b"diff --git ", b"@@")):
            header.append(lines[i])
            i += 1
        changed = []
        while i < len(lines) and lines[i].startswith(b"@@"):
            i = hunk(lines, i, changed)
        files.append((header, changed))
    return files


def hunk(lines, i, changed):
    """Append one hunk's changes to `changed`; returns the index after the hunk.
    A no-newline marker is kept with the changed line it follows; after context it is not a change."""
    match = HUNK.match(lines[i])
    if not match:
        raise Unproven(f"malformed hunk header at line {i + 1}")
    old, old_left, new, new_left = (int(g) if g is not None else 1 for g in match.groups())
    i, previous = i + 1, None
    while old_left or new_left or (i < len(lines) and lines[i].startswith(b"\\")):
        if i >= len(lines):
            raise Unproven("hunk is shorter than its header")
        line, tag = lines[i], lines[i][:1]
        i += 1
        if tag == b" " and old_left and new_left:
            previous = "context"
            old, new, old_left, new_left = old + 1, new + 1, old_left - 1, new_left - 1
        elif tag == b"-" and old_left:
            previous = ("-", old, line[1:])
            changed.append(previous)
            old, old_left = old + 1, old_left - 1
        elif tag == b"+" and new_left:
            previous = ("+", new, line[1:])
            changed.append(previous)
            new, new_left = new + 1, new_left - 1
        elif tag == b"\\" and previous:
            if previous != "context":
                changed.append(("\\", previous[0], previous[1], line))
            previous = None
        else:
            raise Unproven(f"unexpected line {i} in hunk")
    return i
