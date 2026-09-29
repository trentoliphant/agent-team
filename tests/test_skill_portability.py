"""The personal skill must work when installed without the source checkout."""
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from urllib.parse import unquote, urlsplit


SKILL = Path(__file__).resolve().parents[1] / "skills" / "agent-team"


def local_reference_errors(root):
    """Check inline Markdown links/images and reference-style definitions."""
    root = root.resolve()
    errors = []
    for path in root.rglob("*"):
        if path.is_symlink() and (
            not path.resolve().is_relative_to(root) or not path.exists()
        ):
            errors.append(f"{path.name}: broken or escaping symlink")
            continue
        if path.suffix != ".md" or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        targets = re.findall(r"\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)", content)
        targets += re.findall(r"^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?", content, re.M)
        for target in targets:
            url = urlsplit(target)
            if url.scheme in {"https", "http", "mailto"}:
                continue
            if url.scheme or url.netloc:
                errors.append(f"{path.name}: nonportable reference: {target}")
                continue
            if not url.path:
                continue
            dest = (path.parent / unquote(url.path)).resolve()
            if not dest.is_relative_to(root) or not dest.exists():
                errors.append(f"{path.name}: broken or escaping link: {target}")
    return errors


class SkillPortabilityTests(unittest.TestCase):
    def test_standalone_copy_resolves_local_references(self):
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory) / "agent-team"
            shutil.copytree(SKILL, installed, symlinks=True)
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertEqual(local_reference_errors(installed), [])

    def test_reference_check_detects_missing_and_escaping_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skill"
            root.mkdir()
            (Path(directory) / "outside.md").write_text("Outside the skill")
            (root / "inside.md").write_text("Bundled guidance")
            for target, valid in (
                ("inside.md#guidance", True),
                ("https://example.invalid/optional", True),
                ("missing.md", False),
                ("../outside.md", False),
                ("%2e%2e/outside.md", False),
                ("../../README.md", False),
            ):
                for content in (f"[Guidance]({target})", f"[guide]: {target}"):
                    with self.subTest(content=content):
                        (root / "SKILL.md").write_text(content)
                        self.assertEqual(bool(local_reference_errors(root)), not valid)


if __name__ == "__main__":
    unittest.main()
