"""Bounded subprocesses; never interpolate task text into shell commands."""
import os
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
            [str(a) for a in args], cwd=cwd, env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise TeamError(f"Cannot start {args[0]}: {exc}") from exc
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        raise TeamError(f"Interrupted or timed out: {args[0]} (limit {timeout}s)")
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
    return execute(["git", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args]).stdout.strip()
