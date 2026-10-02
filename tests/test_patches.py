"""The review patch: default diff, proven-complete context-free diff, or refusal. Local Git only."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from agent_team.coordinator import review_comment
from agent_team.patches import Unproven, changes, review_patch
from agent_team.process import TeamError, execute, git, git_env

COMMIT = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
ROOT = Path(__file__).resolve().parents[1]
# Source lines that resemble diff syntax once prefixed, plus blank, spaced, and CR-terminated lines.
TRICKY = ["--- a/fake.txt", "+++ b/fake.txt", "-- a/x", "++ b/x", "@@ -1,3 +1,3 @@", "diff --git a/z b/z",
          "\\ No newline at end of file", " leading space", "-minus", "+plus", "", "   ", "carriage\r", "@@"]
# A path Git must C-quote, containing a space, so its `---`/`+++` lines also end with a tab.
QUOTED = 'tab\t"quote" s.txt'


def raw_diff(cwd, base, head, *options):
    return subprocess.run(["git", "-C", str(cwd), "diff", "--no-ext-diff", "--no-color", *options,
                           f"{base}...{head}"], check=True, capture_output=True, env=git_env()).stdout


def fake_git(outputs):
    """A Git runner writing canned diffs: the default diff first, then the --unified=0 diff."""
    def run(cwd, *args):
        out = next(a for a in args if a.startswith("--output=")).split("=", 1)[1]
        Path(out).write_bytes(outputs["--unified=0" in args])
        return ""
    return run


class ReviewPatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        execute(["git", "init", "-b", "main", str(self.repo)])
        self.lines = [f"line {n}" for n in range(400)]

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, files, message):
        for name, content in files.items():
            path = self.repo / name
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, newline="")
        git(self.repo, "add", "-A")
        git(self.repo, *COMMIT, "commit", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def scattered(self):
        """A base and head whose changes are spread through a large file, so context dominates the diff."""
        base = self.commit({"big.txt": "\n".join(self.lines) + "\n"}, "Base")
        edited = [f"edited {n}" if n % 40 == 0 else line for n, line in enumerate(self.lines)]
        return base, self.commit({"big.txt": "\n".join(edited) + "\n"}, "Edit")

    def pathological(self):
        """Changes made of diff-like lines, newline markers, a binary file, a mode change, and a rename."""
        body = "\n".join(self.lines)
        base = self.commit({"big.txt": body + "\n", "tail.txt": "a\nb", "same-tail.txt": "x\nx\nend",
                            "mode.sh": "echo\n", "old-name.txt": "moved\n" * 30}, "Base")
        (self.repo / "mode.sh").chmod(0o755)
        git(self.repo, "mv", "old-name.txt", "new-name.txt")
        edited = list(self.lines)
        for n, line in zip(range(20, 400, 25), TRICKY):
            edited[n] = line
        edited.insert(200, "\n".join(TRICKY))
        head = self.commit({"big.txt": "\n".join(edited), "tail.txt": "a\nc\n",
                            # Unchanged final line without a newline: a marker after context, absent from -U0.
                            "same-tail.txt": "y\nx\nend", "image.bin": bytes(range(256)) * 4,
                            "tricky.txt": "\n".join(TRICKY) + "\n"}, "Pathological")
        return base, head

    def diffs(self, base, head, *options):
        """The default and context-free diffs of `base...head`."""
        return raw_diff(self.repo, base, head, *options), raw_diff(self.repo, base, head, *options, "--unified=0")

    def kinds(self):
        """Every kind of file section Git writes. Empty files are deleted and created in separate
        commits so Git never pairs them as a rename."""
        moved = "".join(f"moved line {n}\n" for n in range(30))
        source = "".join(f"source line {n}\n" for n in range(30))
        base = self.commit({"gone-empty.txt": "", "gone.txt": "bye\n", "chmod.sh": "echo chmod\n",
                            "chmod-edit.sh": "echo before\n", "moved.txt": moved, "source.txt": source,
                            "moved-chmod.txt": "only renamed and made executable\n",
                            "image.bin": bytes(range(256)) * 4, "old.bin": b"\0\1\2\3" * 300,
                            "emptied.txt": "x\ny\n", "filled.txt": "", "sp ace.txt": "space before\n",
                            QUOTED: "quoted before\n"}, "Base")
        link = self.repo / "link"
        link.symlink_to("target-a")
        base = self.commit({}, "Link")
        for name in ("gone-empty.txt", "gone.txt", "old.bin", "moved.txt", "moved-chmod.txt", "link"):
            (self.repo / name).unlink()
        link.symlink_to("target-b")
        mid = self.commit({"renamed.txt": moved.replace("moved line 15\n", "renamed line 15\n"),
                           "moved-chmod.sh": "only renamed and made executable\n", "copy.txt": source,
                           "chmod-edit.sh": "echo after\n", "image.bin": b"\xff" + bytes(range(1, 256)) + bytes(768),
                           "new.bin": b"\0\xfe" * 500, "emptied.txt": "", "filled.txt": "z\n",
                           "sp ace.txt": "space after\n", QUOTED: "quoted after\n"}, "Prepare")
        for name in ("chmod.sh", "chmod-edit.sh", "moved-chmod.sh"):
            (self.repo / name).chmod(0o755)
        mid = self.commit({}, "Kinds")
        return base, mid, self.commit({"new-empty.txt": ""}, "Create empty")

    def test_default_diff_used_when_within_budget(self):
        base, head = self.scattered()
        text, evidence = review_patch(git, self.repo, base, head)
        self.assertEqual(text, raw_diff(self.repo, base, head).decode())
        self.assertEqual((evidence["format"], evidence["complete"], evidence["range"]),
                         ("default", True, f"{base}...{head}"))
        self.assertIn(" line 1\n", text)  # context present

    def test_two_dot_range_compares_the_base_commit(self):
        base, head = self.scattered()
        text, evidence = review_patch(git, self.repo, base, head, separator="..")
        self.assertEqual((text, evidence["range"]), (raw_diff(self.repo, base, head).decode(), f"{base}..{head}"))

    def test_compact_diff_used_only_when_proven_complete(self):
        base, head = self.scattered()
        default, compact = self.diffs(base, head)
        self.assertLess(len(compact), len(default))
        text, evidence = review_patch(git, self.repo, base, head, limit=len(compact))
        self.assertEqual(text, compact.decode())
        self.assertNotIn("\n line ", text)  # no context lines
        self.assertEqual(changes(default), changes(compact, compact=True))
        self.assertEqual({k: evidence[k] for k in ("format", "complete", "files", "changed_lines",
                                                     "default_characters", "characters")},
                         {"format": "compact", "complete": True, "files": 1, "changed_lines": 20,
                          "default_characters": len(default), "characters": len(compact)})
        for n in range(0, 400, 40):
            self.assertIn(f"-line {n}\n+edited {n}\n", text)

    def test_pathological_lines_and_newline_markers_are_proven_equivalent(self):
        base, head = self.pathological()
        default, compact = self.diffs(base, head)
        for text in (b"\\ No newline at end of file", b"Binary files", b"old mode 100644", b"rename from old-name.txt"):
            self.assertIn(text, compact)
        files = changes(compact, compact=True)
        self.assertEqual(files, changes(default))
        # Every tricky line is kept as content of tricky.txt, which is one file with one hunk.
        tricky = next(c for h, c in files if h[0].endswith(b"b/tricky.txt"))
        self.assertEqual([line for _, _, line in tricky], [t.encode() for t in TRICKY])
        self.assertEqual(len(files), 7)
        text, evidence = review_patch(git, self.repo, base, head, limit=len(compact))
        self.assertEqual((text, evidence["format"], evidence["files"]), (compact.decode(), "compact", 7))

    def test_every_legitimate_file_kind_is_accepted_and_proven_equivalent(self):
        base, mid, head = self.kinds()
        expected = {(base, mid, ()): [
                        b"deleted file mode 100644\nindex e69de29..0000000\n", b"\n+++ /dev/null\n",
                        b"old mode 100644\nnew mode 100755\n", b"\nrename from moved.txt\nrename to renamed.txt\nindex ",
                        b"old mode 100644\nnew mode 100755\nsimilarity index 100%\nrename from moved-chmod.txt\n",
                        b"\nBinary files a/image.bin and b/image.bin differ\n",
                        b"\nBinary files a/old.bin and /dev/null differ\n",
                        b"\nBinary files /dev/null and b/new.bin differ\n", b"\n--- /dev/null\n+++ b/copy.txt\n",
                        b"\n@@ -1,2 +0,0 @@\n-x\n-y\n", b"\n@@ -0,0 +1 @@\n+z\n", b"\n--- a/sp ace.txt\t\n",
                        b'\n--- "a/tab\\t\\"quote\\" s.txt"\t\n', b" 120000\n",
                        b"\n-target-a\n\\ No newline at end of file\n+target-b\n\\ No newline at end of file\n"],
                    (base, mid, ("-C", "--find-copies-harder")): [b"\ncopy from source.txt\ncopy to copy.txt\n"],
                    (mid, head, ()): [b"\nnew file mode 100644\nindex 0000000..e69de29\n"]}
        for (old, new, options), present in expected.items():
            with self.subTest(range=(old, new), options=options):
                default, compact = self.diffs(old, new, *options)
                for text in present:
                    self.assertIn(text, default)
                    self.assertIn(text, compact)
                self.assertEqual(changes(default), changes(compact, compact=True))
                if not options:
                    text, evidence = review_patch(git, self.repo, old, new, limit=len(compact))
                    self.assertIn(text, (default.decode(), compact.decode()))
                    self.assertTrue(evidence["complete"])

    def test_still_too_large_is_refused(self):
        base, head = self.scattered()
        compact = self.diffs(base, head)[1]
        with self.assertRaisesRegex(TeamError, "exceeds review budget even without context"):
            review_patch(git, self.repo, base, head, limit=len(compact) - 1)

    def test_mismatch_or_unparsable_compact_patch_fails_closed(self):
        base, head = self.pathological()
        default, compact = self.diffs(base, head)
        marker = b"\n\\ No newline at end of file"
        self.assertIn(marker, compact)
        self.assertIn(b" x\n end" + marker, default)  # a marker after context only
        limit = len(compact) + 100
        self.assertGreater(len(default), limit)
        cases = {
            "changed line differs": compact.replace(b"\n+--- a/fake.txt", b"\n+--- a/fakE.txt", 1),
            "missing newline marker": compact.replace(marker, b"", 1),
            "missing metadata": compact.replace(b"old mode 100644\n", b"", 1),
            "missing file": compact[:compact.rindex(b"\ndiff --git ") + 1],
            "short hunk": compact[:compact.rindex(b"\n", 0, len(compact) - 1) + 1],
            "stray line": compact + b"trailing junk\n",
            "no final newline": compact[:-1],
            "unknown header line": compact.replace(b"\nindex ", b"\nbogus header\nindex ", 1),
            "malformed hunk header": compact.replace(b"\n@@ -", b"\n@@ -x", 1),
            "bogus marker": compact.replace(b"\n+--- a/fake.txt\n", b"\n+--- a/fake.txt\n\\ bogus marker\n", 1),
            "marker before more lines": compact.replace(b"\n+--- a/fake.txt\n", b"\n+--- a/fake.txt" + marker + b"\n",
                                                        1),
        }
        for name, bad in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(bad, compact)
                with self.assertRaisesRegex(TeamError, "Context-free patch .*review refused"):
                    review_patch(fake_git({False: default, True: bad}), self.repo, base, head, limit=limit)
        # The unaltered patch passes through the same runner, so each refusal is due to its alteration.
        self.assertEqual(review_patch(fake_git({False: default, True: compact}), self.repo, base, head,
                                      limit=limit)[1]["format"], "compact")

    def test_malformed_compact_hunks_fail_closed_even_with_matching_changed_lines(self):
        base, head = self.scattered()
        default, compact = self.diffs(base, head)
        limit = len(compact) + 100
        self.assertGreater(len(default), limit)
        hunk = re.compile(rb"@@ -41 \+41 @@[^\n]*\n-line 40\n\+edited 40\n")
        self.assertRegex(compact, hunk)
        # Each alteration keeps every changed line and line number, so only structure can refuse it.
        cases = {
            "unchanged line at zero": compact + b"@@ -0 +0 @@\n invented context\n",
            "context-only hunk": compact + b"@@ -399 +399 @@\n line 398\n",
            "split change with misaligned empty range": hunk.sub(
                b"@@ -41 +41,0 @@\n-line 40\n@@ -40,0 +41 @@\n+edited 40\n", compact, 1),
        }
        for name, bad in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(bad, compact)
                with self.assertRaisesRegex(TeamError, "Context-free patch could not be proven complete"):
                    review_patch(fake_git({False: default, True: bad}), self.repo, base, head, limit=limit)

    def test_malformed_default_patch_fails_closed(self):
        base, head = self.scattered()
        default = raw_diff(self.repo, base, head)
        marker = b"\\ No newline at end of file\n"
        self.assertIn(b"\n line 1\n line 2\n", default)  # context in the middle of a hunk
        cases = {
            "not a patch": b"not a patch\n",
            "bogus marker after context": default.replace(b"\n line 1\n", b"\n line 1\n\\ bogus marker\n", 1),
            "marker after mid-hunk context": default.replace(b"\n line 1\n", b"\n line 1\n" + marker, 1),
            "repeated marker after mid-hunk change": default.replace(b"\n+edited 360\n",
                                                                     b"\n+edited 360\n" + marker * 2, 1),
            "unknown header line": default.replace(b"\nindex ", b"\nbogus header\nindex ", 1),
            "missing hunks": default[:default.index(b"\n@@") + 1],
            "malformed hunk header": default.replace(b"\n@@ -", b"\n@@ -x", 1),
            "file header only": b"diff --git a/f b/f\n",
            "index line without body": default[:default.index(b"\n---") + 1],
            "context-only hunk": default + b"@@ -398,2 +398,2 @@\n line 397\n line 398\n",
        }
        for name, bad in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(bad, default)
                with self.assertRaisesRegex(TeamError, "Diff could not be parsed .*review refused"):
                    review_patch(fake_git({False: bad, True: b""}), self.repo, base, head)
        self.assertEqual(review_patch(fake_git({False: default, True: b""}), self.repo, base, head)[1]["format"],
                         "default")

    def test_partial_rename_or_copy_without_body_fails_closed(self):
        base, head = self.scattered()
        default, compact = self.diffs(base, head)
        limit = len(compact) + 200
        self.assertGreater(len(default), limit)
        for kind in (b"rename", b"copy"):
            section = b"diff --git a/f b/g\nsimilarity index %s%%\n" + kind + b" from f\n" + kind + b" to g\n"
            exact, partial = section % b"100", section % b"90"
            with self.subTest(kind=kind):
                self.assertEqual(len(changes(exact)), 1)
                for patch in (partial, partial.replace(b"90%", b"99%")):
                    for flag in (False, True):
                        with self.assertRaisesRegex(Unproven, "partial rename or copy"):
                            changes(patch, compact=flag)
                with self.assertRaisesRegex(TeamError, "Diff could not be parsed .*review refused"):
                    review_patch(fake_git({False: default + partial, True: b""}), self.repo, base, head)
                with self.assertRaisesRegex(TeamError, "Context-free patch could not be proven complete"):
                    review_patch(fake_git({False: default + exact, True: compact + partial}), self.repo, base, head,
                                 limit=limit)
                # The exact rename or copy passes both paths, so each refusal is due to the partial similarity.
                self.assertEqual(review_patch(fake_git({False: default + exact, True: b""}), self.repo, base,
                                              head)[1]["format"], "default")
                self.assertEqual(review_patch(fake_git({False: default + exact, True: compact + exact}), self.repo,
                                              base, head, limit=limit)[1]["format"], "compact")

    def test_invalid_utf8_is_refused_not_replaced(self):
        base = self.commit({"big.txt": "\n".join(self.lines) + "\n", "latin.txt": b"keep\n\xff\n"}, "Base")
        edited = [f"edited {n}" if n % 40 == 0 else line for n, line in enumerate(self.lines)]
        head = self.commit({"big.txt": "\n".join(edited) + "\n", "latin.txt": b"keep\n\xfe\n"}, "Edit")
        default, compact = self.diffs(base, head)
        self.assertIn(b"\n-\xff\n+\xfe\n", compact)  # a text diff, not a binary one
        for limit in (None, len(compact)):
            with self.subTest(limit=limit), self.assertRaisesRegex(TeamError, "Diff is not valid UTF-8"):
                review_patch(git, self.repo, base, head, limit=limit)
        # A context-free patch with bytes the reviewer cannot receive unchanged is refused too.
        valid = default.replace(b"\xff", b"?").replace(b"\xfe", b"!")
        with self.assertRaisesRegex(TeamError, "Context-free patch is not valid UTF-8"):
            review_patch(fake_git({False: valid, True: compact}), self.repo, base, head, limit=len(compact))

class PatchStructureTests(unittest.TestCase):
    """Synthetic file sections: each refusal is checked against a legitimate section of the same kind."""
    TEXT = (b"diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n"
            b"@@ -2 +2 @@\n-b\n+B\n@@ -9,0 +10,2 @@\n+x\n+y\n")
    HEAD = b"diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n"
    VALID = {
        "text": TEXT,
        "insertion": HEAD + b"@@ -1,0 +2 @@\n+x\n",
        "deletion": HEAD + b"@@ -2 +1,0 @@\n-x\n",
        "created": b"diff --git a/f b/f\nnew file mode 100644\nindex 0000000..2222222\n--- /dev/null\n+++ b/f\n"
                   b"@@ -0,0 +1 @@\n+x\n",
        "deleted": b"diff --git a/f b/f\ndeleted file mode 100644\nindex 1111111..0000000\n--- a/f\n+++ /dev/null\n"
                   b"@@ -1 +0,0 @@\n-x\n",
        "created empty": b"diff --git a/f b/f\nnew file mode 100644\nindex 0000000..e69de29\n",
        "deleted empty": b"diff --git a/f b/f\ndeleted file mode 100755\nindex e69de29..0000000\n",
        "mode only": b"diff --git a/f b/f\nold mode 100644\nnew mode 100755\n",
        "rename": b"diff --git a/f b/g\nsimilarity index 100%\nrename from f\nrename to g\n",
        "copy with mode": b"diff --git a/f b/g\nold mode 100644\nnew mode 100755\nsimilarity index 100%\n"
                          b"copy from f\ncopy to g\n",
        "binary": b"diff --git a/f b/f\nindex 1111111..2222222 100644\nBinary files a/f and b/f differ\n",
        "quoted": b'diff --git "a/f\\tq" "b/f\\303\\251"\nsimilarity index 90%\nrename from "f\\tq"\n'
                  b'rename to "f\\303\\251"\nindex 1111111..2222222 100644\nBinary files "a/f\\tq" and "b/f\\303\\251" differ\n',
        "space": b"diff --git a/s p b/s p\nindex 1111111..2222222 100644\n--- a/s p\t\n+++ b/s p\t\n@@ -1 +1 @@\n-a\n+b\n",
        "marker": HEAD + b"@@ -2 +2 @@\n-b\n\\ No newline at end of file\n+B\n\\ No newline at end of file\n",
    }
    SECTIONS = {
        "not a patch": b"not a patch\n",
        "file header only": b"diff --git a/f b/f\n",
        "index line without body": HEAD[:HEAD.index(b"--- ")],
        "names without hunks": HEAD,
        "created nonempty without body": VALID["created"][:VALID["created"].index(b"--- ")],
        "created empty with body": VALID["created"].replace(b"2222222", b"e69de29"),
        "mode only with hunk": VALID["mode only"] + b"--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n",
        "old mode alone": b"diff --git a/f b/f\nold mode 100644\n",
        "new mode alone": b"diff --git a/f b/f\nnew mode 100755\n",
        "unchanged mode": b"diff --git a/f b/f\nold mode 100644\nnew mode 100644\n",
        "created with mode change": b"diff --git a/f b/f\nnew file mode 100644\nold mode 100644\nnew mode 100755\n"
                                    b"index 0000000..e69de29\n",
        "created and deleted": b"diff --git a/f b/f\nnew file mode 100644\ndeleted file mode 100644\n"
                               b"index 0000000..e69de29\n",
        "misordered metadata": b"diff --git a/f b/f\nindex 0000000..e69de29\nnew file mode 100644\n",
        "repeated index": TEXT.replace(b"100644\n", b"100644\nindex 1111111..2222222 100644\n", 1),
        "rename without similarity": b"diff --git a/f b/g\nrename from f\nrename to g\n",
        "similarity without rename": b"diff --git a/f b/f\nold mode 100644\nnew mode 100755\nsimilarity index 100%\n",
        "rename without destination": b"diff --git a/f b/g\nsimilarity index 100%\nrename from f\n",
        "rename and copy mixed": b"diff --git a/f b/g\nsimilarity index 100%\nrename from f\ncopy to g\n",
        "rename names disagree": b"diff --git a/f b/h\nsimilarity index 100%\nrename from f\nrename to g\n",
        "rename to itself": b"diff --git a/f b/f\nsimilarity index 100%\nrename from f\nrename to f\n",
        "similarity over 100": b"diff --git a/f b/g\nsimilarity index 101%\nrename from f\nrename to g\n",
        "partial rename without body": b"diff --git a/f b/g\nsimilarity index 90%\nrename from f\nrename to g\n",
        "partial copy without body": b"diff --git a/f b/g\nsimilarity index 90%\ncopy from f\ncopy to g\n",
        "partial rename with mode without body": b"diff --git a/f b/g\nold mode 100644\nnew mode 100755\n"
                                                 b"similarity index 99%\nrename from f\nrename to g\n",
        "created and renamed": b"diff --git a/f b/g\nnew file mode 100644\nsimilarity index 100%\nrename from f\n"
                               b"rename to g\nindex 0000000..e69de29\n",
        "names differ without rename": TEXT.replace(b" b/f\n", b" b/g\n", 1),
        "old name differs": TEXT.replace(b"--- a/f\n", b"--- a/g\n"),
        "missing new name": TEXT.replace(b"+++ b/f\n", b""),
        "/dev/null for existing file": TEXT.replace(b"--- a/f\n", b"--- /dev/null\n"),
        "zero hash without creation": TEXT.replace(b"1111111..", b"0000000.."),
        "zero hash without deletion": TEXT.replace(b"..2222222", b"..0000000"),
        "identical hashes": TEXT.replace(b"2222222", b"1111111"),
        "index mode missing": TEXT.replace(b" 100644\n", b"\n", 1),
        "index mode on created file": VALID["created empty"].replace(b"e69de29\n", b"e69de29 100644\n"),
        "index mode with mode change": b"diff --git a/f b/f\nold mode 100644\nnew mode 100755\n"
                                       b"index 1111111..2222222 100755\nBinary files a/f and b/f differ\n",
        "binary names differ": VALID["binary"].replace(b"and b/f", b"and b/g"),
        "binary deletion without metadata": VALID["binary"].replace(b"and b/f", b"and /dev/null"),
        "bad quoted path": VALID["quoted"].replace(b'"a/f\\tq" "b', b'"a/f\\q" "b', 1),
        "space name without tab": VALID["space"].replace(b"--- a/s p\t\n", b"--- a/s p\n"),
        "unknown header line": TEXT.replace(b"\nindex ", b"\nbogus header\nindex ", 1),
    }
    HUNKS = {
        "invented context at line zero": TEXT + b"@@ -0 +0 @@\n invented context\n",
        "context-only hunk": TEXT + b"@@ -20 +22 @@\n same\n",
        "nonempty old range at zero": HEAD + b"@@ -0 +1 @@\n-a\n+b\n",
        "nonempty new range at zero": HEAD + b"@@ -1 +0 @@\n-a\n+b\n",
        "misaligned insertion position": HEAD + b"@@ -2,0 +2 @@\n+x\n",
        "misaligned deletion position": HEAD + b"@@ -2 +2,0 @@\n-x\n",
        "misaligned first hunk": HEAD + b"@@ -2 +3 @@\n-b\n+B\n",
        "overlapping": HEAD + b"@@ -2,2 +2,2 @@\n-b\n-c\n+B\n+C\n@@ -3 +3 @@\n-c\n+C\n",
        "out of order": HEAD + b"@@ -5 +5 @@\n-e\n+E\n@@ -2 +2 @@\n-b\n+B\n",
        "adjacent": HEAD + b"@@ -2 +2 @@\n-b\n+B\n@@ -3 +3 @@\n-c\n+C\n",
        "unequal gap": HEAD + b"@@ -2 +2 @@\n-b\n+B\n@@ -5 +6 @@\n-e\n+E\n",
        "empty hunk": HEAD + b"@@ -2,0 +2,0 @@\n",
        "leading zero": HEAD + b"@@ -02 +2 @@\n-b\n+B\n",
        "hunk after marker": VALID["marker"] + b"@@ -5 +5 @@\n-e\n+E\n",
        "created file with old range": VALID["created"].replace(b"@@ -0,0 +1 @@", b"@@ -1,0 +2 @@"),
        "deleted file with new range": VALID["deleted"].replace(b"@@ -1 +0,0 @@", b"@@ -2 +1,0 @@"),
    }

    def test_legitimate_sections_are_accepted(self):
        for name, patch in self.VALID.items():
            with self.subTest(case=name):
                files = changes(patch, compact=True)
                self.assertEqual(len(files), 1)
                self.assertEqual(changes(patch), files)
        self.assertEqual(changes(b"".join(self.VALID.values())),
                         [file for patch in self.VALID.values() for file in changes(patch)])

    def test_malformed_sections_and_hunks_are_refused(self):
        for name, patch in {**self.SECTIONS, **self.HUNKS}.items():
            for compact in (False, True):
                with self.subTest(case=name, compact=compact), self.assertRaises(Unproven):
                    changes(patch, compact=compact)

    def test_context_free_parsing_refuses_any_unchanged_line(self):
        with_context = self.HEAD + b"@@ -1,3 +1,3 @@\n a\n-b\n+B\n c\n"
        self.assertEqual(changes(with_context), changes(self.HEAD + b"@@ -2 +2 @@\n-b\n+B\n", compact=True))
        with self.assertRaisesRegex(Unproven, "unchanged line"):
            changes(with_context, compact=True)

    def test_hunk_lengths_come_from_headers(self):
        removed = (b"diff --git a/f b/f\nindex 1111111..2222222 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1 @@\n"
                   b"--- a/f\n-+++ b/f\n+diff --git a/g b/g\n")
        self.assertEqual(changes(removed), [([b"diff --git a/f b/f", b"index 1111111..2222222 100644", b"--- a/f",
                                              b"+++ b/f"],
                                             [("-", 1, b"-- a/f"), ("-", 2, b"+++ b/f"),
                                              ("+", 1, b"diff --git a/g b/g")])])
        with self.assertRaises(Unproven):
            changes(b"diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n+c\n")


class ReviewCommentTests(unittest.TestCase):
    RECORD = {"agent": "codex", "family": "openai", "cli_version": "1", "requested_model": "m",
              "observed_models": [], "report": {"verdict": "pass", "summary": "Looks right.", "findings": []}}

    def test_compact_patch_is_disclosed_in_published_review(self):
        patch = {"format": "compact", "default_characters": 250000, "files": 7, "changed_lines": 42}
        body = review_comment("abc", dict(self.RECORD, patch=patch))
        self.assertIn("full diff (250000 characters) exceeded the review budget", body)
        self.assertIn("context-free patch (`--unified=0`, 7 files, 42 changed lines)", body)
        self.assertIn("inspect the full source", body)

    def test_default_patch_adds_nothing(self):
        plain = review_comment("abc", self.RECORD)
        self.assertEqual(review_comment("abc", dict(self.RECORD, patch={"format": "default"})), plain)
        self.assertNotIn("unified", plain)


class ImportTests(unittest.TestCase):
    def test_modules_import_safely_in_either_order(self):
        for first, second in (("agent_team.patches", "agent_team.coordinator"),
                              ("agent_team.coordinator", "agent_team.patches")):
            with self.subTest(first=first):
                code = (f"import {first}, {second}, agent_team.cli\n"
                        "from agent_team import coordinator, patches\n"
                        "assert coordinator.review_patch is patches.review_patch\n"
                        "assert coordinator.COMPACT_NOTICE is patches.COMPACT_NOTICE\n")
                result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
