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
NUMBER = rb"(0|[1-9][0-9]*)"
HUNK = re.compile(rb"@@ -" + NUMBER + rb"(?:," + NUMBER + rb")? \+" + NUMBER + rb"(?:," + NUMBER + rb")? @@(?: .*)?")
MARKER = b"\\ No newline at end of file"
# Git's extended header lines in the order Git writes them. Lines of equal rank are alternatives.
METADATA = ((0, "created", re.compile(rb"new file mode ([0-7]{6})")),
            (0, "deleted", re.compile(rb"deleted file mode ([0-7]{6})")),
            (1, "old_mode", re.compile(rb"old mode ([0-7]{6})")),
            (2, "new_mode", re.compile(rb"new mode ([0-7]{6})")),
            (3, "similarity", re.compile(rb"similarity index ([0-9]{1,3})%")),
            (3, "dissimilarity", re.compile(rb"dissimilarity index ([0-9]{1,3})%")),
            (4, "from", re.compile(rb"(rename|copy) from (.+)")),
            (5, "to", re.compile(rb"(rename|copy) to (.+)")),
            (6, "index", re.compile(rb"index ([0-9a-f]{4,64})\.\.([0-9a-f]{4,64})(?: ([0-7]{6}))?")))
# The empty blob in SHA-1 and SHA-256 repositories: a created or deleted empty file has no body.
EMPTY_BLOBS = (b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
               b"473a0f4c3be8a93681a267e3b1e9a7dcda1185436fe141f7749120a303721813")
ESCAPES = {ord(k): v for k, v in zip('abtnvfr"\\', b'\a\b\t\n\v\f\r"\\')}
OCTAL = re.compile(rb"[0-3][0-7]{2}")
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
        same = files == changes(compact, compact=True)
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


def changes(raw, compact=False):
    """Each file's exact header lines and its changed lines with their line numbers, independent
    of context and hunk layout. Every file section must be complete and consistent: metadata in Git's
    order without contradictions, then exactly the body that metadata requires (none, a binary notice,
    or `---`/`+++` names followed by ordered hunks). Hunk lengths come from hunk headers, so source lines
    that resemble diff syntax (`--- a`, `+++ b`, `@@`, `diff --git`, `\\`) remain content.
    A `compact` (context-free) patch may not contain any unchanged line."""
    if raw and not raw.endswith(b"\n"):
        raise Unproven("patch does not end with a newline")
    lines, files, i = raw.split(b"\n")[:-1], [], 0
    while i < len(lines):
        if not lines[i].startswith(b"diff --git "):
            raise Unproven(f"line {i + 1} is not a file header")
        start, i, meta, rank = i, i + 1, {}, -1
        while i < len(lines):
            found = [(r, key, m) for r, key, pattern in METADATA if (m := pattern.fullmatch(lines[i]))]
            if not found:
                break
            (r, key, m), = found
            if r <= rank:
                raise Unproven(f"repeated, contradictory, or misordered metadata at line {i + 1}")
            meta[key], rank, i = m.groups(), r, i + 1
        old, new, bodyless = section(lines[start], meta, start + 1)
        header, changed = lines[start:i], []
        body = i < len(lines) and lines[i].startswith((b"Binary files ", b"--- "))
        if body == bodyless:
            raise Unproven(f"file at line {start + 1} " + ("has a body its metadata does not allow" if body else
                                                           "has a content change but no binary notice or hunks"))
        if body and lines[i].startswith(b"Binary files "):
            if lines[i] != b"Binary files " + old + b" and " + new + b" differ":
                raise Unproven(f"binary notice at line {i + 1} does not match the file header")
            header.append(lines[i])
            i += 1
        elif body:
            if lines[i] != label(b"--- ", old) or i + 1 >= len(lines) or lines[i + 1] != label(b"+++ ", new):
                raise Unproven(f"file names at line {i + 1} do not match the file header")
            header += lines[i:i + 2]
            i += 2
            if i >= len(lines) or not lines[i].startswith(b"@@"):
                raise Unproven(f"file at line {i} has no hunks")
            after, sealed = None, False
            while i < len(lines) and lines[i].startswith(b"@@"):
                if sealed:
                    raise Unproven(f"hunk at line {i + 1} follows a no-newline marker")
                i, after, sealed = hunk(lines, i, changed, after, compact, "created" in meta, "deleted" in meta)
        files.append((header, changed))
    return files


def section(line, meta, at):
    """The old and new names Git writes for a file section and whether the section has no body.
    Refuses missing or contradictory metadata, and file names that disagree with it."""
    created, deleted, chmod = "created" in meta, "deleted" in meta, "old_mode" in meta
    if chmod != ("new_mode" in meta) or (chmod and (created or deleted or meta["old_mode"] == meta["new_mode"])):
        raise Unproven(f"file at line {at} has incomplete or contradictory mode metadata")
    moved = None
    if not ("similarity" in meta) == ("from" in meta) == ("to" in meta):
        raise Unproven(f"file at line {at} has incomplete rename or copy metadata")
    if "from" in meta:
        if meta["from"][0] != meta["to"][0] or created or deleted:
            raise Unproven(f"file at line {at} has contradictory rename or copy metadata")
        moved = (unquote(meta["from"][1]), unquote(meta["to"][1]))
        if moved[0] == moved[1]:
            raise Unproven(f"file at line {at} is renamed or copied to itself")
    if any(int(meta[key][0]) > 100 for key in ("similarity", "dissimilarity") if key in meta) or \
            ("dissimilarity" in meta and (created or deleted)):
        raise Unproven(f"file at line {at} has an invalid similarity index")
    index = meta.get("index")
    if index is None:
        # Without an index line the content is unchanged, so only a mode change or exact rename/copy remains.
        if created or deleted or "dissimilarity" in meta or not (chmod or moved):
            raise Unproven(f"file at line {at} has no recorded change")
        if moved and meta["similarity"][0] != b"100":
            raise Unproven(f"file at line {at} is a partial rename or copy without an index line and body")
        bodyless = True
    else:
        before, after, mode = index
        if before == after or (not before.strip(b"0")) != created or (not after.strip(b"0")) != deleted:
            raise Unproven(f"index line of file at line {at} contradicts its metadata")
        # Git appends the mode only when both sides exist and share it.
        if (mode is None) != (created or deleted or chmod):
            raise Unproven(f"index mode of file at line {at} contradicts its metadata")
        bodyless = (created and empty(after)) or (deleted and empty(before))
    old, new = names(line, moved, at)
    return (b"/dev/null" if created else old), (b"/dev/null" if deleted else new), bodyless


def names(line, moved, at):
    """The exact `a/` and `b/` path tokens of a `diff --git` line. `moved` holds the unquoted rename or
    copy source and destination; otherwise both tokens name the same path, so exactly one split fits."""
    rest, found = line[len(b"diff --git "):], []
    for k in (k for k, c in enumerate(rest) if c == 0x20):
        try:
            paths = (unquote(rest[:k], b"a/"), unquote(rest[k + 1:], b"b/"))
        except Unproven:
            continue
        if paths == moved if moved else paths[0] == paths[1]:
            found.append((rest[:k], rest[k + 1:]))
    if len(found) != 1:
        raise Unproven(f"file header at line {at} does not name the file consistently")
    return found[0]


def unquote(token, prefix=b""):
    """The path in a Git path token, which Git C-quotes when it contains special bytes, without `prefix`."""
    if token.startswith(b'"'):
        if len(token) < 2 or not token.endswith(b'"'):
            raise Unproven("unterminated quoted path")
        body, out, k = token[1:-1], bytearray(), 0
        while k < len(body):
            if body[k] == 0x22:
                raise Unproven("unescaped quote in quoted path")
            if body[k] != 0x5c:
                out.append(body[k])
                k += 1
            elif k + 1 < len(body) and body[k + 1] in ESCAPES:
                out.append(ESCAPES[body[k + 1]])
                k += 2
            elif OCTAL.fullmatch(body[k + 1:k + 4]):
                out.append(int(body[k + 1:k + 4], 8))
                k += 4
            else:
                raise Unproven("invalid escape in quoted path")
        token = bytes(out)
    if not token.startswith(prefix) or len(token) == len(prefix):
        raise Unproven("path lacks its prefix")
    return token[len(prefix):]


def label(prefix, name):
    """A `---`/`+++` line. Git ends it with a tab when the name contains a space."""
    return prefix + name + (b"\t" if b" " in name else b"")


def empty(abbrev):
    return any(blob.startswith(abbrev) for blob in EMPTY_BLOBS)


def hunk(lines, i, changed, after, compact, created, deleted):
    """Append one hunk's changes to `changed`. Returns the index after the hunk, the next line number
    on each side, and whether a no-newline marker ended a side (so no hunk may follow).
    `after` is the previous hunk's result, or None for a file's first hunk. Lines between hunks are
    unchanged, so both sides skip the same number of them, and at least one (Git merges adjacent hunks).
    A no-newline marker must follow the last line of the side it ends. It is kept with the changed line
    it follows; after context it is not a change."""
    match = HUNK.fullmatch(lines[i])
    if not match:
        raise Unproven(f"malformed hunk header at line {i + 1}")
    old, old_left, new, new_left = (int(g) if g is not None else 1 for g in match.groups())
    at = i + 1
    if not (old_left or new_left):
        raise Unproven(f"empty hunk at line {at}")
    if (old_left and not old) or (new_left and not new):
        raise Unproven(f"nonempty range starting at line zero at line {at}")
    if (created and (old, old_left) != (0, 0)) or (deleted and (new, new_left) != (0, 0)):
        raise Unproven(f"hunk at line {at} contradicts a created or deleted file")
    # An empty range names the line before it, so its position is one line later.
    old, new = old + (not old_left), new + (not new_left)
    skipped = (old - after[0], new - after[1]) if after else (old - 1, new - 1)
    if skipped[0] != skipped[1] or (after and skipped[0] < 1):
        raise Unproven(f"hunk at line {at} overlaps, is out of order, or misaligns unchanged lines")
    end, ended, modified = (old + old_left, new + new_left), set(), False
    i, previous = i + 1, None
    while old_left or new_left or (i < len(lines) and lines[i].startswith(b"\\")):
        if i >= len(lines):
            raise Unproven("hunk is shorter than its header")
        line, tag = lines[i], lines[i][:1]
        i += 1
        sides = {b" ": {"old", "new"}, b"-": {"old"}, b"+": {"new"}}.get(tag, set())
        if sides & ended:
            raise Unproven(f"line {i} follows a no-newline marker for its side")
        if tag == b" " and compact:
            raise Unproven(f"unchanged line {i} in a context-free patch")
        modified = modified or tag in (b"-", b"+")
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
    if not modified:
        raise Unproven(f"hunk at line {at} has no changes")
    return i, end, bool(ended)
