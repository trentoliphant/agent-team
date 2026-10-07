"""Companion repositories: operator-declared, public, and pinned to immutable commits.

The operator declares every companion at registration. A pin comes from the registration or
from a manifest committed in the primary repository; the manifest can pin declared companions
only. Checkouts keep each repository's basename as siblings, so relative paths such as
`../companion` resolve the same way in author, validation, and review workspaces."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile

from .process import TeamError, assert_metadata, execute, git, metadata, substitutions

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
        text = git(checkout, "--no-replace-objects", "show", f"{rev}:{path}")
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


def paths(root, pins):
    return [root / basename(p["repo"]) for p in pins]


def anonymous_env(home):
    """Environment for a clone with no local credential source: an empty home, so Git's HTTP
    transport finds no `.netrc` and no user configuration, and no forwarded tokens."""
    allowed = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home), "NO_COLOR": "1",
                "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def fetch(url, destination, timeout):
    """Clone `url` without credentials: no credential helper and a fresh, empty home."""
    with tempfile.TemporaryDirectory(prefix="agent-team-anonymous-") as home:
        execute(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "init.templateDir=",
                 "clone", "--no-checkout", url, str(destination)], timeout=timeout, env=anonymous_env(home))


def clone(repo, destination, timeout):
    """Anonymous HTTPS clone, so only public repositories are obtainable."""
    fetch(f"https://github.com/{repo}.git", destination, timeout)


def snapshot(checkout):
    """Every path in the working tree outside `.git`, read from the filesystem rather than
    through Git, so index flags such as assume-unchanged or skip-worktree and ignore rules
    cannot hide an edited or added file."""
    return tree(checkout, {".git"})


def git_tree(checkout):
    """Every path in `.git` except the index, which Git read commands may rewrite. Replacement
    refs, grafts, alternates, and added or edited objects or refs all change it, even if they
    are removed after use: objects they wrote remain."""
    return tree(checkout / ".git", {"index"})


def replacement_refs(checkout):
    """Replacement refs as Git lists them, for ref storage that `substitutions` cannot read."""
    return git(checkout, "--no-replace-objects", "for-each-ref", "--format=%(refname)", "refs/replace")


def index(checkout):
    """Staged entries (mode, object, stage, path) as read from the original objects."""
    return git(checkout, "--no-replace-objects", "ls-files", "--stage")


def tree(top, skip):
    """Filesystem contents under `top`, skipping the named top-level entries."""
    entries = {}
    for directory, dirs, files in os.walk(top):
        base = Path(directory)
        if base == top:
            dirs[:] = [d for d in dirs if d not in skip]
            files = [f for f in files if f not in skip]
        for name in dirs + files:
            path = base / name
            rel = path.relative_to(top).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                entries[rel] = ["link", os.readlink(path)]
            elif stat.S_ISDIR(info.st_mode):
                entries[rel] = ["dir"]
            elif stat.S_ISREG(info.st_mode):
                content = hashlib.sha256()
                with path.open("rb") as handle:
                    for block in iter(lambda: handle.read(1 << 20), b""):
                        content.update(block)
                entries[rel] = ["file", bool(info.st_mode & 0o111), content.hexdigest()]
            else:
                entries[rel] = ["other", stat.S_IFMT(info.st_mode)]
    # os.walk does not follow symlinked directories; they are recorded as links above.
    return entries


def populate(root, pins, timeout):
    """Fresh sibling checkouts of each companion at its pin, under `root`. Returns each
    checkout's Git metadata and working-tree contents so `verify` can check them before
    running Git there again."""
    baselines = {}
    for pin in pins:
        destination = root / basename(pin["repo"])
        if destination.exists():
            raise TeamError(f"Companion checkout {destination.name} already exists; inspect before retry")
        clone(pin["repo"], destination, timeout)
        if substitutions(destination):
            raise TeamError(f"Companion {pin['repo']} clone substitutes Git objects or history; refused")
        try:
            git(destination, "--no-replace-objects", "-c", "advice.detachedHead=false",
                "checkout", "--detach", pin["rev"])
        except TeamError:
            raise TeamError(f"Companion {pin['repo']} revision {pin['rev']} is not published") from None
        # A fresh checkout with an untouched index, so a clean status means the files are the pin's.
        if (git(destination, "--no-replace-objects", "rev-parse", "HEAD") != pin["rev"]
                or git(destination, "--no-replace-objects", "status", "--porcelain")
                or replacement_refs(destination)):
            raise TeamError(f"Companion {pin['repo']} checkout does not match pin {pin['rev']}")
        # Taken after the last Git command that may rewrite the index.
        baselines[pin["repo"]] = {"git": metadata(destination), "files": snapshot(destination),
                                  "store": git_tree(destination), "index": index(destination)}
    return baselines


def verify(root, pins, baselines):
    """Refuse evidence from companion checkouts that no longer match their pins: replaced
    directories, changed Git configuration, a moved HEAD, changed, hidden, ignored, or added
    files, or Git objects, refs, replacement refs, grafts, or alternates that make Git reads
    return other contents than the pin's. Contents are compared with snapshots taken at
    checkout, not with Git's view, which index flags, ignore rules, and replacement refs can
    change. Configuration and the object store are checked before Git runs in a checkout that
    commands could edit, and that Git ignores replacement refs."""
    for pin in pins:
        destination = root / basename(pin["repo"])
        changed = TeamError(f"Companion {pin['repo']} changed from pin {pin['rev']} during the stage; "
                            "dependency evidence rejected, inspect before retry")
        if destination.is_symlink() or not destination.is_dir():
            raise changed
        baseline = baselines[pin["repo"]]
        try:
            assert_metadata(destination, baseline["git"])
            if substitutions(destination):
                raise changed
            files = snapshot(destination)
            objects = git_tree(destination)
        except (TeamError, OSError):
            raise changed from None
        if files != baseline["files"] or objects != baseline["store"]:
            raise changed
        if (git(destination, "--no-replace-objects", "rev-parse", "HEAD") != pin["rev"]
                or replacement_refs(destination) or index(destination) != baseline["index"]):
            raise changed
