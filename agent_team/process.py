"""Bounded subprocesses; never interpolate task text into shell commands."""
import os
import hashlib
from contextlib import suppress, ExitStack
from pathlib import Path
import signal
import subprocess


class TeamError(Exception):
    pass


class QuotaError(TeamError):
    pass


class ModelCapacityError(QuotaError):
    """Explicit transient provider saturation, distinct from subscription exhaustion."""


def execute(args, *, cwd=None, env=None, input=None, timeout=120, check=True,
            stdout_path=None, stderr_path=None):
    """Write optional logs directly while the child runs; preserve them on interruption."""
    with ExitStack() as files:
        def output(path):
            if path is None:
                return subprocess.PIPE
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            return files.enter_context(path.open("w", encoding="utf-8"))
        return _execute(args, cwd=cwd, env=env, input=input, timeout=timeout, check=check,
                        stdout=output(stdout_path), stderr=output(stderr_path),
                        stdout_path=stdout_path, stderr_path=stderr_path)


def _execute(args, *, cwd, env, input, timeout, check, stdout, stderr, stdout_path, stderr_path):
    try:
        proc = subprocess.Popen(
            [str(a) for a in args], cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
    except OSError as exc:
        raise TeamError(f"Cannot start {args[0]}: {exc}") from exc
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise TeamError(f"Timed out: {args[0]} (limit {timeout}s)")
    if stdout_path is not None:
        out = Path(stdout_path).read_text(encoding="utf-8", errors="replace")
    if stderr_path is not None:
        err = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
    result = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check and proc.returncode:
        raise TeamError(f"{args[0]} failed ({proc.returncode}): {(err or out)[-3000:]}")
    return result


def worker_env():
    # Deliberate allowlist: retain OS/keychain access for CLI subscription login,
    # never forward API keys, cloud-provider selection, or GitHub bearer tokens.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG",
               "LC_ALL", "TERM", "SYSTEMROOT", "XDG_CONFIG_HOME", "CODEX_HOME",
               "CLAUDE_CONFIG_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {k: v for k, v in os.environ.items() if k in allowed}
    env.update({"NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def git(cwd: Path, *args):
    return execute(["git", "--no-replace-objects", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-c", "core.untrackedCache=false", "-C", cwd, *args], env=git_env()).stdout.strip()


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    return env


def substitutions(checkout):
    """Replacement refs, grafts, shallow boundaries, and alternate object stores, found on the
    filesystem without running Git."""
    root = checkout / ".git"
    found = [rel for rel in ("info/grafts", "shallow", "objects/info/alternates", "objects/info/http-alternates") if os.path.lexists(root / rel)]
    replace = root / "refs" / "replace"
    if replace.is_symlink() or (replace.is_dir() and any(replace.rglob("*"))):
        found.append("refs/replace")
    packed = root / "packed-refs"
    if packed.is_symlink() or (packed.is_file() and b" refs/replace/" in packed.read_bytes()):
        found.append("packed-refs")
    return found


def metadata(cwd):
    """Read configuration directly, before invoking Git against a worker checkout."""
    root = Path(cwd) / ".git"
    if root.is_symlink() or not root.is_dir():
        raise TeamError("Checkout Git metadata must be a real directory")
    if substitutes := substitutions(Path(cwd)):
        raise TeamError("Git history substitutions are not allowed: " + ", ".join(substitutes))
    result = {}
    for rel in ("config", "info/exclude", "info/attributes"):
        path = root / rel
        if path.is_symlink() or path.parent.is_symlink():
            raise TeamError("Symlinked Git configuration is not allowed")
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return result


def assert_metadata(cwd, expected):
    if metadata(cwd) != expected:
        raise TeamError("Checkout Git configuration changed; inspect before any Git operations")


def clone_repository(repo, destination, base, timeout):
    # Force HTTPS/gh credentials so an app token is not accidentally replaced by
    # a personal SSH identity selected in the operator's global gh settings.
    execute(["git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
             "-c", "credential.helper=!gh auth git-credential", "clone", "--branch", base,
             f"https://github.com/{repo}.git", str(destination)], timeout=timeout, env=git_env())
