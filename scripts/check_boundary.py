#!/usr/bin/env python3
"""Check that published source is portable and self-contained."""
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    paths = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                     "--exclude-standard"]).decode().split("\0")
    errors = []
    machine = re.compile(r"/(?:Volumes/[^/]+/Users|Users|home)/[^\s/]+/")
    for rel in sorted(set(filter(None, paths))):
        path = root / rel
        if path.is_symlink():
            if not path.resolve().is_relative_to(root) or not path.exists():
                errors.append(f"{rel}: escaping or broken symlink")
            continue
        try:
            text = path.read_text()
        except (UnicodeError, IsADirectoryError):
            continue
        if machine.search(text):
            errors.append(f"{rel}: machine-specific absolute path")
        if path.suffix == ".md":
            for target in re.findall(r"\]\(<?([^\s)>]+)>?\)", text):
                url = urlsplit(target)
                if url.scheme or url.netloc or not url.path:
                    continue
                dest = (path.parent / unquote(url.path)).resolve()
                if not dest.is_relative_to(root) or not dest.exists():
                    errors.append(f"{rel}: broken or escaping link: {target}")
    return errors


if __name__ == "__main__":
    problems = check()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit(1)
    print("Self-contained source checks passed.")
