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
from . import writing
from .writing import KINDS


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
    configure.add_argument("--max-quota-retries", type=int)
    configure.add_argument("--codex-model")
    configure.add_argument("--claude-model")
    writing = commands.add_parser("writing", help="Writing standards for generated GitHub text").add_subparsers(
        dest="writing_command", required=True)
    show = writing.add_parser("show", help="Show the effective standard and the source of each value")
    show.add_argument("--project", help="Include this project's overrides")
    show.add_argument("--kind", choices=list(KINDS), help="Also print the prompt text for this kind")
    change = writing.add_parser("set", help="Set personal defaults, or project overrides with --project")
    change.add_argument("--project")
    change.add_argument("--shared", help="Instructions for every kind")
    change.add_argument("--kind", choices=list(KINDS))
    change.add_argument("--instructions", help="Instructions for --kind")
    change.add_argument("--words", type=int, help="Approximate word target for --kind; 0 means no target")
    unset = writing.add_parser("unset", help="Remove settings so lower-precedence values apply")
    unset.add_argument("--project")
    unset.add_argument("--shared", action="store_true")
    unset.add_argument("--kind", choices=list(KINDS))
    unset.add_argument("--field", choices=["instructions", "words"], help="Only this field of --kind")
    unset.add_argument("--all", action="store_true", help="Every setting at this level")
    run = commands.add_parser("run", help="Advance one stage, or poll with --watch")
    run.add_argument("project")
    run.add_argument("--issue", type=int, help="Select only this approved ready issue")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--interval", type=int, default=30)
    queue = commands.add_parser("queue", help="Inspect or save a project's implementation order").add_subparsers(
        dest="queue_command", required=True)
    for verb in ("show", "set", "reorder", "clear"):
        item = queue.add_parser(verb)
        item.add_argument("project")
        if verb in {"set", "reorder"}:
            item.add_argument("order", help="Comma-separated positive issue numbers")
    approve = commands.add_parser("approve", help="Approve current issue content and add the ready label")
    approve.add_argument("project")
    approve.add_argument("issue", type=int)
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
                project = store.pause(args.name, verb == "pause")
            elif verb == "configure":
                for key in ("timeout", "max_revisions", "quota_cooldown", "max_quota_retries", "codex_model", "claude_model"):
                    value = getattr(args, key)
                    if value is not None:
                        minimum = 0 if key == "max_revisions" else 1
                        if isinstance(value, int) and value < minimum:
                            raise TeamError(f"{key} must be at least {minimum}")
                        project[key] = value
                store.save_project(project)
            emit(project)
    elif args.command == "writing":
        project = store.project(args.project) if args.project else None
        if args.writing_command == "set":
            changed = writing.update(project.get("writing") if project else store.writing(),
                                     args.shared, args.kind, args.instructions, args.words)
        elif args.writing_command == "unset":
            changed = writing.remove(project.get("writing") if project else store.writing(),
                                     args.shared, args.kind, args.field, args.all)
        if args.writing_command != "show":
            if project:
                project["writing"] = changed
                store.save_project(project)
            else:
                store.save_writing(changed)
        policy, sources = writing.effective(store.writing(), project.get("writing") if project else None)
        result = {"project": args.project, "precedence": writing.PRECEDENCE,
                  "effective": policy, "sources": sources}
        if args.writing_command == "show" and args.kind:
            result["prompt"] = writing.guidance(policy, args.kind)
        emit(result)
    elif args.command == "queue":
        if args.queue_command in {"set", "reorder"}:
            try:
                numbers = [int(n.strip()) for n in args.order.split(",")]
            except ValueError as exc:
                raise TeamError("Order must be comma-separated positive issue numbers") from exc
            if args.queue_command == "reorder" and set(numbers) != set(
                    store.project(args.project).get("queue_order", [])):
                raise TeamError("Reorder must contain exactly the saved entries; use set to replace them")
            store.set_queue(args.project, numbers)
        elif args.queue_command == "clear":
            store.set_queue(args.project, [])
        if args.queue_command == "show":
            emit(team.queue(args.project))
        else:
            emit({"project": args.project, "saved_order": store.project(args.project)["queue_order"]})
    elif args.command == "approve":
        emit(github.approve(store.project(args.project), args.issue))
    elif args.command == "run":
        if args.interval < 1:
            raise TeamError("Polling interval must be positive")
        # Lock per tick, not across sleep, so pause/status remain usable.
        while True:
            with store.lock():
                value = team.tick(args.project, args.issue)
            emit({k: value[k] for k in ("id", "project", "stage", "error", "pr") if k in value})
            if not args.watch or (args.issue is not None and value["stage"] in {
                    "ready", "stale", "blocked", "waiting", "paused", "closed", "merged", "idle"}):
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
                print("No runs. Approve a registered project's issue with: agent-team approve PROJECT NUMBER")
    elif args.command == "inspect":
        emit(store.get(args.run_id))
    elif args.command == "resume":
        run = store.get(args.run_id)
        if run["stage"] not in {"blocked", "quota_wait"}:
            raise TeamError("Only blocked or quota-waiting runs can be resumed")
        store.save(run, stage=run["resume_stage"], error=None, in_flight=False, quota_attempts=0)
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
        if (args.command in {"run", "status", "inspect", "doctor", "smoke", "init"} or
                (args.command == "project" and args.project_command in {"pause", "resume", "list", "show"}) or
                (args.command == "writing" and args.writing_command == "show") or
                (args.command == "queue" and args.queue_command == "show")):
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
