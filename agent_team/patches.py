"""The complete patch an independent reviewer receives, within the review budget.

The default diff is used when it fits. Otherwise a context-free diff (`--unified=0`) is used only
after it is proven to have exactly the same file headers and changed lines as the default diff.
Either patch is parsed before use and must be valid UTF-8, so the reviewer receives exactly its bytes.
Nothing is ever truncated, sliced, or excluded; anything that cannot be proven is refused."""
import hashlib
from pathlib import Path
import re
import tempfile

from .process import TeamError

REVIEW_BUDGET = 180000
HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")
MARKER = b"\\ No newline at end of file"
# Git's file header and extended header lines. Paths may be quoted; a trailing tab may follow `---`/`+++` paths.
FILE = re.compile(rb'diff --git "?a/.+ "?b/.+')
EXTENDED = re.compile(rb"(?:old|new|deleted file|new file) mode [0-7]{6}|(?:copy|rename) (?:from|to) .+|"
                      rb"(?:dis)?similarity index \d{1,3}%|index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?")
OLD = re.compile(rb'--- (?:/dev/null|"?a/.+)')
NEW = re.compile(rb'\+\+\+ (?:/dev/null|"?b/.+)')
BINARY = re.compile(rb'Binary files (?:/dev/null|"?a/.+) and (?:/dev/null|"?b/.+) differ')
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
    text = decode(default, "Diff")
    try:
        files = changes(default)
    except Unproven as exc:
        raise TeamError(f"Diff could not be parsed ({exc}); review refused") from exc
    evidence = {"range": span, "format": "default", "context": "git default", "complete": True,
                "characters": len(text), "default_characters": len(text),
                "sha256": hashlib.sha256(default).hexdigest(), "files": len(files),
                "changed_lines": count(files), "encoding": "utf-8, decoded strictly"}
    if len(text) <= limit:
        return text, evidence
    compact = diff(git, cwd, span, "--unified=0")
    short = decode(compact, "Context-free patch")
    if len(short) > limit:
        raise TeamError(f"Diff exceeds review budget even without context ({len(short)} of {limit} characters); "
                        "split the PR")
    try:
        same = files == changes(compact)
    except Unproven as exc:
        raise TeamError(f"Context-free patch could not be proven complete ({exc}); review refused") from exc
    if not same:
        raise TeamError("Context-free patch differs from the full diff; review refused")
    return short, dict(evidence, format="compact", context="none (--unified=0)", characters=len(short),
                       sha256=hashlib.sha256(compact).hexdigest(),
                       equivalence="same file headers and changed lines as the default diff")


def decode(raw, name):
    """The exact text of `raw`. Bytes that are not UTF-8 cannot reach the reviewer unchanged, so they are refused."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TeamError(f"{name} is not valid UTF-8 (byte {exc.start}), so it cannot be given to the reviewer "
                        "without loss; review refused") from exc


def count(files):
    return sum(1 for _, lines in files for c in lines if c[0] != "\\")


def diff(git, cwd, span, *options):
    """Raw diff bytes. Written to a file outside the checkout so no output is stripped or decoded."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "patch"
        git(cwd, "diff", "--no-ext-diff", "--no-color", *options, f"--output={out}", span)
        return out.read_bytes()


def changes(raw):
    """Each file's exact header lines and its changed lines with their line numbers, independent
    of context and hunk layout. Every line must be a recognised Git header, hunk header, hunk line,
    or correctly placed no-newline marker. Hunk lengths come from hunk headers, so source lines that
    resemble diff syntax (`--- a`, `+++ b`, `@@`, `diff --git`, `\\`) remain content."""
    if raw and not raw.endswith(b"\n"):
        raise Unproven("patch does not end with a newline")
    lines, files, i = raw.split(b"\n")[:-1], [], 0
    while i < len(lines):
        if not FILE.fullmatch(lines[i]):
            raise Unproven(f"line {i + 1} is not a file header")
        header, i = [lines[i]], i + 1
        while i < len(lines) and EXTENDED.fullmatch(lines[i]):
            header.append(lines[i])
            i += 1
        changed, ended = [], set()
        if i < len(lines) and BINARY.fullmatch(lines[i]):
            header.append(lines[i])
            i += 1
        elif i + 1 < len(lines) and OLD.fullmatch(lines[i]) and NEW.fullmatch(lines[i + 1]):
            header += lines[i:i + 2]
            i += 2
            if i >= len(lines) or not lines[i].startswith(b"@@"):
                raise Unproven(f"file at line {i} has no hunks")
            while i < len(lines) and lines[i].startswith(b"@@"):
                i = hunk(lines, i, changed, ended)
        files.append((header, changed))
    return files


def hunk(lines, i, changed, ended):
    """Append one hunk's changes to `changed`; returns the index after the hunk.
    A no-newline marker must follow the last line of the side it ends (`ended` records it for the file).
    It is kept with the changed line it follows; after context it is not a change."""
    match = HUNK.fullmatch(lines[i])
    if not match:
        raise Unproven(f"malformed hunk header at line {i + 1}")
    old, old_left, new, new_left = (int(g) if g is not None else 1 for g in match.groups())
    if not (old_left or new_left):
        raise Unproven(f"empty hunk at line {i + 1}")
    i, previous = i + 1, None
    while old_left or new_left or (i < len(lines) and lines[i].startswith(b"\\")):
        if i >= len(lines):
            raise Unproven("hunk is shorter than its header")
        line, tag = lines[i], lines[i][:1]
        i += 1
        sides = {b" ": {"old", "new"}, b"-": {"old"}, b"+": {"new"}}.get(tag, set())
        if sides & ended:
            raise Unproven(f"line {i} follows a no-newline marker for its side")
        if tag == b" " and old_left and new_left:
            old, new, old_left, new_left = old + 1, new + 1, old_left - 1, new_left - 1
        elif tag == b"-" and old_left:
            changed.append(("-", old, line[1:]))
            old, old_left = old + 1, old_left - 1
        elif tag == b"+" and new_left:
            changed.append(("+", new, line[1:]))
            new, new_left = new + 1, new_left - 1
        elif tag == b"\\" and line == MARKER and previous:
            # The marked line must be the last line of its side, so that side has no lines left.
            if (previous == b" " and (old_left or new_left)) or (previous == b"-" and old_left) or \
                    (previous == b"+" and new_left):
                raise Unproven(f"misplaced no-newline marker at line {i}")
            ended.update({b" ": {"old", "new"}, b"-": {"old"}, b"+": {"new"}}[previous])
            if previous != b" ":
                changed.append(("\\", changed[-1][0], changed[-1][1], line))
            previous = None
            continue
        else:
            raise Unproven(f"unexpected line {i} in hunk")
        previous = tag
    return i
