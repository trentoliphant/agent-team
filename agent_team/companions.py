"""Companion repositories: operator-declared, public, and pinned to immutable commits.

The operator declares every companion at registration. A pin comes from the registration or
from a manifest committed in the primary repository; the manifest can pin declared companions
only. Checkouts keep each repository's basename as siblings, so relative paths such as
`../companion` resolve the same way in author, validation, and review workspaces."""
import hashlib
import json
from pathlib import PurePosixPath
import re

from .process import TeamError, execute, git, worker_env

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REVISION = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def basename(repo):
    return repo.split("/", 1)[1]


def check_repo(repo):
    if not isinstance(repo, str) or not REPO.fullmatch(repo) or ".." in repo:
        raise TeamError(f"Companion repository must be OWNER/REPO on github.com: {repo!r}")
    return repo


def check_revision(repo, rev):
    # Branch and tag names move; only a full commit SHA is an immutable pin.
    if not isinstance(rev, str) or not REVISION.fullmatch(rev):
        raise TeamError(f"Companion {repo} must be pinned to a full lowercase commit SHA, not {rev!r}")
    return rev


def parse(values):
    """Command-line declarations: `OWNER/REPO` (pinned by the manifest) or `OWNER/REPO@SHA`."""
    declared = []
    for value in values:
        repo, _, rev = value.partition("@")
        declared.append({"repo": check_repo(repo), "rev": check_revision(repo, rev) if rev else None})
    return declared


def configure(primary, companions, manifest):
    """Validate a project's companion configuration before it is saved."""
    names = {basename(primary).casefold()}
    for item in companions:
        check_repo(item["repo"])
        if item.get("rev") is not None:
            check_revision(item["repo"], item["rev"])
        # Case-insensitive, because checkouts share one directory on case-insensitive filesystems.
        name = basename(item["repo"]).casefold()
        if name in names:
            raise TeamError(f"Companion basename {basename(item['repo'])} must differ from the primary "
                            "repository and every other companion")
        names.add(name)
    if manifest is not None:
        path = PurePosixPath(manifest)
        if not companions:
            raise TeamError("A companion manifest only pins declared companions; declare them with --companion")
        if not manifest or "\\" in manifest or path.is_absolute() or ".." in path.parts:
            raise TeamError("Companion manifest must be a relative path inside the repository")


def read_manifest(checkout, rev, path):
    """Manifest entries committed at `rev`. Ignored or uncommitted files are never read."""
    try:
        text = git(checkout, "show", f"{rev}:{path}")
    except TeamError:
        raise TeamError(f"Companion manifest {path} is not committed at {rev}; companion pins are missing") from None
    try:
        entries = json.loads(text)["companions"]
        return [{"repo": check_repo(e["repo"]), "rev": check_revision(e["repo"], e["rev"])} for e in entries]
    except (ValueError, KeyError, TypeError) as exc:
        raise TeamError(f"Companion manifest {path} must be JSON: "
                        '{"companions": [{"repo": "OWNER/REPO", "rev": "SHA"}]}') from exc


def resolve(project, entries=()):
    """Pins for every declared companion. Manifest pins replace registration pins."""
    declared = {c["repo"].casefold(): dict(c) for c in project.get("companions", [])}
    seen = set()
    for entry in entries:
        key = entry["repo"].casefold()
        if key not in declared:
            raise TeamError(f"Manifest names undeclared companion {entry['repo']}; only the operator declares companions")
        if key in seen:
            raise TeamError(f"Manifest pins companion {entry['repo']} more than once")
        seen.add(key)
        declared[key]["rev"] = entry["rev"]
    pins = [{"repo": c["repo"], "rev": c["rev"]} for c in declared.values()]
    missing = [p["repo"] for p in pins if not p["rev"]]
    if missing:
        raise TeamError(f"Missing companion pin for {', '.join(missing)}; pin a commit SHA at registration "
                        "or in the committed manifest")
    return pins


def digest(pins):
    return hashlib.sha256(json.dumps(pins, sort_keys=True).encode()).hexdigest()[:12]


def text(pins):
    return "; ".join(f"`{p['repo']}` at `{p['rev']}`" for p in pins)


def clone(repo, destination, timeout):
    """Anonymous HTTPS clone: no credential helper and no forwarded tokens, so only public
    repositories are obtainable."""
    execute(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "init.templateDir=",
             "clone", "--no-checkout", f"https://github.com/{repo}.git", str(destination)],
            timeout=timeout, env=worker_env())


def populate(root, pins, timeout):
    """Fresh sibling checkouts of each companion at its pin, under `root`."""
    for pin in pins:
        destination = root / basename(pin["repo"])
        if destination.exists():
            raise TeamError(f"Companion checkout {destination.name} already exists; inspect before retry")
        clone(pin["repo"], destination, timeout)
        try:
            git(destination, "-c", "advice.detachedHead=false", "checkout", "--detach", pin["rev"])
        except TeamError:
            raise TeamError(f"Companion {pin['repo']} revision {pin['rev']} is not published") from None
        if git(destination, "rev-parse", "HEAD") != pin["rev"]:
            raise TeamError(f"Companion {pin['repo']} checkout does not match pin {pin['rev']}")
