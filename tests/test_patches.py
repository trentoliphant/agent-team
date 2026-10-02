"""The review patch: default diff, proven-complete context-free diff, or refusal. Local Git only."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_team.patches import Unproven, changes, review_patch
from agent_team.process import TeamError, execute, git, git_env

COMMIT = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]
ROOT = Path(__file__).resolve().parents[1]
# Source lines that resemble diff syntax once prefixed, plus blank, spaced, and CR-terminated lines.
TRICKY = ["--- a/fake.txt", "+++ b/fake.txt", "-- a/x", "++ b/x", "@@ -1,3 +1,3 @@", "diff --git a/z b/z",
          "\\ No newline at end of file", " leading space", "-minus", "+plus", "", "   ", "carriage\r", "@@"]


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

    def diffs(self, base, head):
        """The default and context-free diffs of `base...head`."""
        return raw_diff(self.repo, base, head), raw_diff(self.repo, base, head, "--unified=0")

    def test_default_diff_used_when_within_budget(self):
        base, head = self.scattered()
        text, evidence = review_patch(git, self.repo, base, head)
        self.assertEqual(text, raw_diff(self.repo, base, head).decode())
        self.assertEqual((evidence["format"], evidence["complete"], evidence["range"]),
                         ("default", True, f"{base}...{head}"))
        self.assertIn(" line 1\n", text)  # context present

    def test_compact_diff_used_only_when_proven_complete(self):
        base, head = self.scattered()
        default, compact = self.diffs(base, head)
        self.assertLess(len(compact), len(default))
        text, evidence = review_patch(git, self.repo, base, head, limit=len(compact))
        self.assertEqual(text, compact.decode())
        self.assertNotIn("\n line ", text)  # no context lines
        self.assertEqual(changes(default), changes(compact))
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
        files = changes(compact)
        self.assertEqual(files, changes(default))
        # Every tricky line is kept as content of tricky.txt, which is one file with one hunk.
        tricky = next(c for h, c in files if h[0].endswith(b"b/tricky.txt"))
        self.assertEqual([line for _, _, line in tricky], [t.encode() for t in TRICKY])
        self.assertEqual(len(files), 7)
        text, evidence = review_patch(git, self.repo, base, head, limit=len(compact))
        self.assertEqual((text, evidence["format"], evidence["files"]), (compact.decode(), "compact", 7))

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
        }
        for name, bad in cases.items():
            with self.subTest(case=name):
                self.assertNotEqual(bad, compact)
                with self.assertRaisesRegex(TeamError, "Context-free patch .*review refused"):
                    review_patch(fake_git({False: default, True: bad}), self.repo, base, head, limit=limit)
        # The unaltered patch passes through the same runner, so each refusal is due to its alteration.
        self.assertEqual(review_patch(fake_git({False: default, True: compact}), self.repo, base, head,
                                      limit=limit)[1]["format"], "compact")

    def test_hunk_lengths_come_from_headers(self):
        removed = (b"diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n@@ -1,2 +1 @@\n"
                   b"--- a/f\n-+++ b/f\n+diff --git a/g b/g\n")
        self.assertEqual(changes(removed), [([b"diff --git a/f b/f", b"index 1..2 100644", b"--- a/f", b"+++ b/f"],
                                             [("-", 1, b"-- a/f"), ("-", 2, b"+++ b/f"),
                                              ("+", 1, b"diff --git a/g b/g")])])
        with self.assertRaises(Unproven):
            changes(b"diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n+c\n")


class ImportTests(unittest.TestCase):
    def test_modules_import_safely_in_either_order(self):
        for first, second in (("agent_team.pull_requests", "agent_team.coordinator"),
                              ("agent_team.coordinator", "agent_team.pull_requests")):
            with self.subTest(first=first):
                code = (f"import {first}, {second}, agent_team.cli\n"
                        "from agent_team import coordinator, pull_requests\n"
                        "assert pull_requests.core is coordinator\n"
                        "assert coordinator.PullRequests is pull_requests.PullRequests\n"
                        "assert coordinator.pull_number is pull_requests.pull_number\n")
                result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                                        timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
