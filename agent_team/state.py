"""Single-host durable registry and run journal, outside source checkouts."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .process import TeamError

ACTIVE = {"prepare", "implement", "validate", "publish", "review", "ci"}
TERMINAL = {"merged", "closed"}
# Runs waiting for an operator: they stop new assignments and never advance on their own.
RECOVERY = {"blocked", "quota_wait", "handoff", "repair"}


def issue_fingerprint(issue):
    value = {"title": issue["title"], "body": issue.get("body") or ""}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def default_home():
    return Path(os.environ.get("AGENT_TEAM_HOME", str(
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-team"
    ))).expanduser().resolve()


class Store:
    def __init__(self, home):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / "state.sqlite3", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projects(name TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, project TEXT NOT NULL,
                issue INTEGER NOT NULL, data TEXT NOT NULL, UNIQUE(project, issue));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT,
                at REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.commit()

    @contextmanager
    def lock(self):
        with (self.home / "coordinator.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TeamError("Another coordinator is running with this state directory") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def projects(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM projects ORDER BY name")]

    def project(self, name):
        row = self.db.execute("SELECT data FROM projects WHERE name=?", (name,)).fetchone()
        if not row:
            raise TeamError(f"Unknown project: {name}")
        return json.loads(row[0])

    def save_project(self, project):
        self.db.execute("INSERT OR REPLACE INTO projects VALUES (?, ?)",
                        (project["name"], json.dumps(project)))
        self.db.commit()

    def pause(self, name, paused):
        # Short independent transaction: works while an agent holds the worker lock.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            project = self.project(name)
            project["paused"] = paused
            self.db.execute("UPDATE projects SET data=? WHERE name=?", (json.dumps(project), name))
        return project

    def register(self, name, repo, base, tests, **options):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise TeamError("Project name must contain only letters, digits, underscore, or dash")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo:
            raise TeamError("Repository must be OWNER/REPO on github.com")
        if any(p["name"] == name or p["repo"].lower() == repo.lower() for p in self.projects()):
            raise TeamError("Project name or repository is already registered")
        if not tests:
            raise TeamError("At least one --test command is required")
        project = dict(name=name, repo=repo, base=base, tests=tests, paused=False,
                       ready_label="agent:ready", timeout=1800, max_revisions=2,
                       quota_cooldown=3600, max_quota_retries=3, codex_model=None, claude_model=None)
        project.update(options)
        self.save_project(project)
        return project

    def set_queue(self, name, numbers):
        numbers = list(numbers)
        if any(type(n) is not int or n < 1 for n in numbers):
            raise TeamError("Queue entries must be positive issue numbers")
        if len(numbers) != len(set(numbers)):
            raise TeamError("Duplicate queue entries are not allowed")
        project = self.project(name)
        project["queue_order"] = numbers
        self.save_project(project)
        return numbers

    def writing(self):
        """Personal writing defaults for every project in this state directory."""
        row = self.db.execute("SELECT value FROM meta WHERE key='writing'").fetchone()
        return json.loads(row[0]) if row else {}

    def save_writing(self, layer):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('writing', ?)", (json.dumps(layer),))
        self.db.commit()

    def runs(self, project=None):
        rows = self.db.execute("SELECT data FROM runs" + (" WHERE project=?" if project else ""),
                               (project,) if project else ())
        return [json.loads(r[0]) for r in rows]

    def get(self, run_id):
        row = self.db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise TeamError(f"Unknown run: {run_id}")
        return json.loads(row[0])

    def save(self, run, **changes):
        run.update(changes)
        run["updated"] = time.time()
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?)",
                        (run["id"], run["project"], run["issue"], json.dumps(run)))
        self.db.execute("INSERT INTO events(run,at,data) VALUES (?,?,?)",
                        (run["id"], time.time(), json.dumps(changes)))
        self.db.commit()

    def create(self, project, issue):
        key = "author_rotation"
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        index = int(row[0]) if row else 0
        author = ["codex", "claude"][index % 2]
        run = dict(id=uuid.uuid4().hex[:12], project=project["name"],
                   issue=issue["number"], title=issue["title"], body=issue.get("body") or "",
                   author=author, reviewer="claude" if author == "codex" else "codex",
                   stage="prepare", round=0, pr=None, sha=None, base_sha=None,
                   in_flight=False, feedback="", created=time.time())
        run.update(issue_digest=issue_fingerprint(issue), quota_attempts=0, needs_revision=False)
        run["branch"] = f"agent-team/{run['issue']}-{run['id']}"
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                        (run["id"], run["project"], run["issue"], json.dumps(run)))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(index + 1)))
        self.db.commit()
        return run

    def workspace(self, run):
        return self.home / "runs" / run["id"] / "author"

    def artifacts(self, run):
        path = self.home / "runs" / run["id"] / "artifacts"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
