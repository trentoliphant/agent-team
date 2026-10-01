"""Command-line interface. Each run tick advances one durable stage."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from . import __version__
from . import companions
from .agents import Agents, subscription_status
from .coordinator import ACTIONS, CONTRIBUTORS, MAX_EXTENSION, STOP_POINTS, ENTRY_POINTS, EFFECTS, Coordinator
from .github import GitHub
from .process import TeamError, execute
from .state import CoordinatorBusy, Store, default_home
from . import writing
from .writing import KINDS


def companion_options(command):
    command.add_argument("--companion", action="append", metavar="OWNER/REPO[@SHA]",
                         help="Public companion repository cloned beside the checkout at a pinned commit; "
                              "repeatable, and replaces the declared list")
    command.add_argument("--companion-manifest", metavar="PATH",
                         help="Committed JSON file in the repository that pins declared companions")


def public_companions(github, values):
    """Declared companions must be public: they are cloned anonymously at each run."""
    declared = companions.parse(values)
    for item in declared:
        repo = github.repo(item["repo"])
        if repo.get("private") or repo.get("visibility", "public") != "public":
            raise TeamError(f"Companion {item['repo']} must be a public repository")
        item["repo"] = repo["full_name"]
    return declared


def parser():
    root = argparse.ArgumentParser(description="Local subscription-backed agent teams; GitHub tracking; human merges")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--home", type=Path, default=default_home(), help="External state directory")
    commands = root.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Configure single-host worker capacity")
    configure.add_argument("--concurrency", type=int, required=True)
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
    companion_options(add)
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
    companion_options(configure)
    configure.add_argument("--no-companions", action="store_true",
                           help="Remove companions and the manifest; later runs are single-repository")
    writing =commands.add_parser("writing", help="Writing standards for generated GitHub text").add_subparsers(
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
    run.add_argument("--stop-after", choices=STOP_POINTS, help="Persist an endpoint for --issue; later ticks cannot advance beyond it")
    run.add_argument("--watch", action="store_true")
    run.add_argument("--interval", type=int, default=30)
    run.add_argument("--run", dest="run_id", help="Advance only this tracked run, including task runs")
    selection = commands.add_parser("select", help="Save explicit operations, scope, grants and endpoint; no execution")
    selection.add_argument("project")
    selection.add_argument("--operations", nargs="+", choices=ENTRY_POINTS, required=True)
    selection.add_argument("--grant", action="append", choices=EFFECTS, default=[])
    selection.add_argument("--issue", type=int)
    selection.add_argument("--task", help="Explicit immutable task scope and acceptance criteria")
    selection.add_argument("--ref", help="Existing remote branch or exact commit")
    selection.add_argument("--run", dest="run_id", help="Continue compatible tracked evidence")
    selection.add_argument("--contributor", action="append", choices=CONTRIBUTORS, default=[])
    selection.add_argument("--plan", action="store_true", help="Show operations and effects without saving or executing")
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
        if verb == "refresh":
            item.add_argument("--contributor", action="append", choices=CONTRIBUTORS, default=[])
            item.add_argument("--grant", choices=["edit"], help="Explicit local base-integration authorization")
    continuation = commands.add_parser("continue", help="Explicitly continue a stopped run without resetting its history")
    continuation.add_argument("run_id")
    continuation.add_argument("--operations", nargs="+", choices=ENTRY_POINTS, required=True)
    continuation.add_argument("--contributor", action="append", choices=list(CONTRIBUTORS), default=[])
    handoff = commands.add_parser("handoff", help="Show the latest revision-limit handoff and decisions")
    handoff.add_argument("run_id")
    handoff.add_argument("--json", action="store_true")
    decide = commands.add_parser("decide", help="Record an operator decision for a handoff or recovery run")
    decide.add_argument("run_id")
    decide.add_argument("action", choices=list(ACTIONS))
    decide.add_argument("--revisions", type=int, help=f"Finite extension for extend (1-{MAX_EXTENSION})")
    decide.add_argument("--note", default="", help="Published with the decision")
    adopt = commands.add_parser("adopt", help="Adopt a direct repair (PR head, or local repair checkout "
                                                "if never published) for new validation and review")
    adopt.add_argument("run_id")
    adopt.add_argument("--contributor", action="append", choices=list(CONTRIBUTORS), required=True,
                       help="Who contributed to the repair; repeatable")
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
    if args.command == "configure":
        store.set_concurrency(args.concurrency)
        emit({"concurrency": store.concurrency()})
    elif args.command == "init":
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
            with store.subscription(args.agent):
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
            options = {}
            # Single-repository registrations store no companion settings at all.
            if args.companion:
                options["companions"] = public_companions(github, args.companion)
            if args.companion_manifest is not None:
                options["companion_manifest"] = args.companion_manifest
            emit(store.register(args.name, repo["full_name"], args.base or repo["default_branch"], args.test,
                                ready_label=args.ready_label, codex_model=args.codex_model,
                                claude_model=args.claude_model, **options))
        else:
            project = store.project(args.name)
            if verb == "setup":
                github.setup(project)
            elif verb in {"pause", "resume"}:
                project = store.pause(args.name, verb == "pause")
            elif verb == "configure":
                changes = {}
                for key in ("timeout", "max_revisions", "quota_cooldown", "max_quota_retries", "codex_model", "claude_model"):
                    value = getattr(args, key)
                    if value is not None:
                        minimum = 0 if key == "max_revisions" else 1
                        if isinstance(value, int) and value < minimum:
                            raise TeamError(f"{key} must be at least {minimum}")
                        changes[key] = value
                if args.no_companions and (args.companion or args.companion_manifest is not None):
                    raise TeamError("--no-companions cannot be combined with companion settings")
                remove = ("companions", "companion_manifest") if args.no_companions else ()
                if args.companion:
                    changes["companions"] = public_companions(github, args.companion)
                if args.companion_manifest is not None:
                    changes["companion_manifest"] = args.companion_manifest
                project = store.update_project(
                    args.name, remove=remove, check=lambda p: companions.configure(
                        p["repo"], p.get("companions", []), p.get("companion_manifest")), **changes)
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
                project = store.update_project(args.project, writing=changed)
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
    elif args.command == "select":
        grants = sorted(set(args.grant) | set(store.get(args.run_id).get("grants", []) if args.run_id else []))
        plan = team.operation_plan(args.operations, grants)
        emit(plan)
        if not args.plan:
            emit(team.select(args.project, args.operations, args.grant, args.issue, args.task,
                             args.ref, args.contributor, args.run_id))
    elif args.command == "run":
        if args.run_id and (args.issue is not None or args.stop_after is not None):
            raise TeamError("Use --run alone to retain the saved plan")
        if args.interval < 1:
            raise TeamError("Polling interval must be positive")
        # Lock per tick, not across sleep, so pause/status remain usable.
        while True:
            try:
                if args.run_id:
                    value = team.tick(args.project, run_id=args.run_id)
                elif args.stop_after is None:
                    value = team.tick(args.project, args.issue)
                else:
                    value = team.tick(args.project, args.issue, args.stop_after)
            except CoordinatorBusy as exc:
                value = {"project": args.project, "stage": "busy", "error": str(exc)}
            emit({k: value[k] for k in ("id", "project", "stage", "error", "pr") if k in value})
            if not args.watch or ((args.issue is not None or args.run_id) and value["stage"] in {
                    "stopped", "ready", "stale", "blocked", "handoff", "repair", "waiting", "paused", "closed", "merged",
                    "idle"}):
                break
            time.sleep(args.interval)
    elif args.command == "status":
        runs = store.runs(args.project)
        if args.json:
            emit(runs)
        else:
            for run in runs:
                scope = f"#{run['issue']}" if run["issue"] is not None else "task"
                print(f"{run['id']}  {run['project']}  {scope}  {run['stage']}  "
                      f"{run['author']} → {run['reviewer']}  PR {run.get('pr') or '-'}")
            if not runs:
                print("No runs. Approve a registered project's issue with: agent-team approve PROJECT NUMBER")
    elif args.command == "inspect":
        emit(store.get(args.run_id))
    elif args.command == "continue":
        emit(team.continue_run(args.run_id, args.operations, args.contributor))
    elif args.command == "resume":
        emit(team.resume(args.run_id))
    elif args.command == "close":
        run = store.get(args.run_id)
        store.save(run, stage="closed", in_flight=False, notification_pending=True)
        team.notify(store.project(run["project"]), run)
        emit({"id": run["id"], "stage": "closed", "note": "GitHub issue/PR and local checkout retained"})
    elif args.command == "refresh":
        emit(team.refresh(args.run_id, args.contributor, args.grant == "edit"))
    elif args.command == "handoff":
        run = store.get(args.run_id)
        if not run.get("handoffs"):
            raise TeamError("No handoff recorded for this run")
        if args.json:
            emit({k: run.get(k) for k in ("id", "stage", "handoffs", "revision_history", "decisions",
                                          "adoptions", "contributors", "rejected_shas")})
        else:
            print(run["handoffs"][-1]["text"])
            for decision in run.get("decisions", []):
                print(f"\nDecision after revision {decision['round']}: {decision['action']}"
                      + (f" ({decision['revisions']} more)" if decision["revisions"] else ""))
            if run.get("repair_checkout"):
                print(f"\nLocal repair checkout: {run['repair_checkout']['path']}\n"
                      "Commit repairs there on top of the rejected commit, then run agent-team adopt.")
            print(f"\nCurrent stage: {run['stage']}")
    elif args.command == "decide":
        run = team.decide(args.run_id, args.action, args.revisions, args.note)
        emit({k: run.get(k) for k in ("id", "stage", "round", "revision_limit", "extension", "decisions",
                                      "repair_checkout")})
    elif args.command == "adopt":
        run = team.adopt(args.run_id, args.contributor)
        emit({k: run.get(k) for k in ("id", "stage", "round", "sha", "contributors", "adoptions")})
    elif args.command == "discover":
        emit(team.discover(args.project, args.agent, args.focus))
    return 0


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    store = Store(args.home)
    try:
        if args.command in {"continue", "resume", "close", "decide", "adopt", "refresh"}:
            with store.repository_lock(store.get(args.run_id)["project"]):
                code = dispatch(args, store)
        elif args.command == "select":
            with store.repository_lock(args.project):
                code = dispatch(args, store)
        elif args.command == "discover":
            with store.worker(args.project):
                code = dispatch(args, store)
        elif (args.command == "approve" or
              (args.command == "queue" and args.queue_command != "show")):
            with store.repository_lock(args.project):
                code = dispatch(args, store)
        elif args.command == "project" and args.project_command == "setup":
            with store.repository_lock(args.name):
                code = dispatch(args, store)
        elif (args.command in {"run", "status", "inspect", "handoff", "doctor", "smoke", "init"} or
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
