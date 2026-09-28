"""Bounded subprocesses; never interpolate task text into shell commands."""
import os
import hashlib
from pathlib import Path
import signal
import subprocess


class TeamError(Exception):
    pass


class QuotaError(TeamError):
    pass


def execute(args, *, cwd=None, env=None, input=None, timeout=120, check=True):
    try:
        proc = subprocess.Popen(
            [str(a) for a in args], cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise TeamError(f"Cannot start {args[0]}: {exc}") from exc
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise TeamError(f"Timed out: {args[0]} (limit {timeout}s)")
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
    return execute(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                    "-c", "core.untrackedCache=false", "-C", cwd, *args], env=git_env()).stdout.strip()


def git_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return env


def metadata(cwd):
    """Read configuration directly, before invoking Git against a worker checkout."""
    root = Path(cwd) / ".git"
    if root.is_symlink() or not root.is_dir():
        raise TeamError("Checkout Git metadata must be a real directory")
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
