"""Command-line interface. Each run tick advances one durable stage."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from . import __version__
from .agents import Agents, subscription_status
from .coordinator import Coordinator
from .github import GitHub
from .process import TeamError, execute
from .state import Store, default_home


def parser():
    root = argparse.ArgumentParser(description="Local subscription-backed agent teams; GitHub tracking; human merges")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, default=default_home(), help="External state directory")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize local registry and state")
    commands.add_parser("doctor", help="Check CLI subscription logins and GitHub authentication, without inference")
    smoke = commands.add_parser("smoke", help="Small real subscription call; no GitHub writes")
    smoke.add_argument("--agent", choices=["codex", "claude"], required=True)
    project = commands.add_parser("project").add_subparsers(dest="project_command", required=True)
    add = project.add_parser("add", help="Register a trusted repository; does not change repository files")
    add.add_argument("name")
    add.add_argument("repo", help="OWNER/REPO")
    add.add_argument("--base")
    add.add_argument("--test", action="append", required=True, help="Trusted shell command executed in checkout; repeatable")
    add.add_argument("--ready-label", default="agent:ready")
    add.add_argument("--codex-model")
    add.add_argument("--claude-model")
    project.add_parser("list")
    for verb in ("show", "setup", "pause", "resume"):
        item = project.add_parser(verb)
        item.add_argument("name")
    configure = project.add_parser("configure")
    configure.add_argument("name")
    configure.add_argument("--timeout", type=int)
    configure.add_argument("--max-revisions", type=int)
    configure.add_argument("--quota-cooldown", type=int)
    configure.add_argument("--codex-model")
    configure.add_argument("--claude-model")
    run = commands.add_parser("run", help="Advance one stage, or poll with --watch")
    run.add_argument("project")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--interval", type=int, default=30)
    status = commands.add_parser("status")
    status.add_argument("--project")
    status.add_argument("--json", action="store_true")
    for verb in ("inspect", "resume", "close", "refresh"):
        item = commands.add_parser(verb)
        item.add_argument("run_id")
    discover = commands.add_parser("discover", help="Read-only investigation; opens at most three unready issues")
    discover.add_argument("project")
    discover.add_argument("--agent", choices=["codex", "claude"], default="claude")
    discover.add_argument("--focus", default="Correctness, onboarding, and missing acceptance tests")
    return root


def emit(value):
    print(json.dumps(value, indent=2, ensure_ascii=False), flush=True)


def dispatch(args, store):
    github = GitHub()
    team = Coordinator(store, github)
    if args.command == "init":
        emit({"home": str(store.home), "version": __version__})
    elif args.command == "doctor":
        results = {}
        failed = False
        for agent in ["codex", "claude"]:
            try:
                results[agent] = {"subscription": True, "version": subscription_status(agent)}
            except TeamError as exc:
                results[agent] = {"subscription": False, "error": str(exc)}
                failed = True
        try:
            results["github"] = {"login": github.login()}
        except TeamError as exc:
            results["github"] = {"error": str(exc)}
            failed = True
        emit(results)
        if failed:
            return 1
    elif args.command == "smoke":
        with tempfile.TemporaryDirectory(prefix="agent-team-smoke-") as directory:
            cwd = Path(directory) / "checkout"
            cwd.mkdir()
            execute(["git", "init", str(cwd)])
            (cwd / "hello.txt").write_text("subscription smoke test\n")
            emit(Agents().run(args.agent, "review",
                             "Read hello.txt. Report pass with empty findings if it says subscription smoke test. "
                             "Do not call any other tools or change files. Return the requested structured report.",
                             cwd, Path(directory) / "artifacts", {"timeout": 180}))
    elif args.command == "project":
        verb = args.project_command
        if verb == "list":
            emit(store.projects())
        elif verb == "add":
            repo = github.repo(args.repo)
            if repo.get("archived"):
                raise TeamError("Cannot register an archived repository")
            emit(store.register(args.name, repo["full_name"], args.base or repo["default_branch"], args.test,
                                ready_label=args.ready_label, codex_model=args.codex_model,
                                claude_model=args.claude_model))
        else:
            project = store.project(args.name)
            if verb == "setup":
                github.setup(project)
            elif verb in {"pause", "resume"}:
                project["paused"] = verb == "pause"
                store.save_project(project)
            elif verb == "configure":
                for key in ("timeout", "max_revisions", "quota_cooldown", "codex_model", "claude_model"):
                    value = getattr(args, key)
                    if value is not None:
                        minimum = 0 if key == "max_revisions" else 1
                        if isinstance(value, int) and value < minimum:
                            raise TeamError(f"{key} must be at least {minimum}")
                        project[key] = value
                store.save_project(project)
            emit(project)
    elif args.command == "run":
        if args.interval < 1:
            raise TeamError("Polling interval must be positive")
        # Lock per tick, not across sleep, so pause/status remain usable.
        while True:
            with store.lock():
                value = team.tick(args.project)
            emit({k: value[k] for k in ("id", "project", "stage", "error", "pr") if k in value})
            if not args.watch:
                break
            time.sleep(args.interval)
    elif args.command == "status":
        runs = store.runs(args.project)
        if args.json:
            emit(runs)
        else:
            for run in runs:
                print(f"{run['id']}  {run['project']}  #{run['issue']}  {run['stage']}  "
                      f"{run['author']} → {run['reviewer']}  PR {run.get('pr') or '-'}")
            if not runs:
                print("No runs. Register a project and label an issue agent:ready.")
    elif args.command == "inspect":
        emit(store.get(args.run_id))
    elif args.command == "resume":
        run = store.get(args.run_id)
        if run["stage"] not in {"blocked", "quota_wait"}:
            raise TeamError("Only blocked or quota-waiting runs can be resumed")
        store.save(run, stage=run["resume_stage"], error=None, in_flight=False)
        emit(run)
    elif args.command == "close":
        run = store.get(args.run_id)
        store.save(run, stage="closed", in_flight=False, notification_pending=True)
        team.notify(store.project(run["project"]), run)
        emit({"id": run["id"], "stage": "closed", "note": "GitHub issue/PR and local checkout retained"})
    elif args.command == "refresh":
        emit(team.refresh(args.run_id))
    elif args.command == "discover":
        emit(team.discover(args.project, args.agent, args.focus))
    return 0


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = Store(args.home)
    try:
        if args.command in {"run", "status", "inspect", "doctor", "smoke", "init"}:
            code = dispatch(args, store)
        else:
            with store.lock():
                code = dispatch(args, store)
    except (TeamError, ValueError) as exc:
        print(f"agent-team: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        print("Stopped. State retained; inspect before resuming.", file=sys.stderr)
        code = 130
    finally:
        store.db.close()
    raise SystemExit(code)
