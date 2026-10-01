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

from .companions import basename, configure as configure_companions
from .process import TeamError, QuotaError

ACTIVE = {"discovery", "issue_prepare", "revision", "prepare", "implement", "validate", "publish", "review", "checks", "ci"}
TERMINAL = {"merged", "closed"}
# Runs waiting for an operator: they stop new assignments and never advance on their own.
RECOVERY = {"blocked", "quota_wait", "handoff", "repair", "stopped"}


def issue_fingerprint(issue):
    value = {"title": issue["title"], "body": issue.get("body") or ""}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def default_home():
    return Path(os.environ.get("AGENT_TEAM_HOME", str(
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "agent-team"
    ))).expanduser().resolve()


class CoordinatorBusy(TeamError):
    pass


class CapacityWait(TeamError):
    def __init__(self, retry_at):
        super().__init__("Shared subscription capacity is busy or cooling down")
        self.retry_at = retry_at


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
                issue INTEGER, data TEXT NOT NULL, UNIQUE(project, issue));
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, run TEXT,
                at REAL NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # Older registries required a GitHub issue for every run. NULL identifies
        # an explicitly scoped local task, never a fabricated GitHub issue.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = self.db.execute("PRAGMA table_info(runs)").fetchall()
            if next(c for c in columns if c["name"] == "issue")["notnull"]:
                self.db.execute("ALTER TABLE runs RENAME TO issue_runs")
                self.db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY, project TEXT NOT NULL, "
                                "issue INTEGER, data TEXT NOT NULL, UNIQUE(project, issue))")
                self.db.execute("INSERT INTO runs SELECT * FROM issue_runs")
                self.db.execute("DROP TABLE issue_runs")
        self.db.commit()

    @contextmanager
    def file_lock(self, name, shared=False):
        # Never unlink lock files: all processes must lock the same inode.
        with (self.home / name).open("a") as handle:
            try:
                fcntl.flock(handle, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                message = ("Repository already has a live coordinator worker" if name.startswith("repository-")
                           else "Another coordinator is running with this state directory")
                raise CoordinatorBusy(message) from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def lock(self):
        """Exclusive administrative gate; workers hold shared access."""
        return self.file_lock("coordinator.lock")

    def concurrency(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='concurrency'").fetchone()
        return int(row[0]) if row else 1

    def set_concurrency(self, limit):
        if type(limit) is not int or limit < 1:
            raise TeamError("Concurrency must be a positive integer")
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('concurrency', ?)", (str(limit),))
        self.db.commit()

    @contextmanager
    def repository_lock(self, name):
        """Exclude work on this repository without consuming a worker slot."""
        with self.file_lock("coordinator.lock", shared=True):
            repo = self.project(name)["repo"].lower()
            digest = hashlib.sha256(repo.encode()).hexdigest()
            with self.file_lock(f"repository-{digest}.lock"):
                yield

    @contextmanager
    def worker(self, name):
        with self.repository_lock(name):
            slot = None
            for index in range(self.concurrency()):
                candidate = self.file_lock(f"worker-{index}.lock")
                try:
                    candidate.__enter__()
                except TeamError:
                    continue
                slot = candidate
                break
            if slot is None:
                raise CoordinatorBusy("Coordinator concurrency limit reached")
            try:
                yield
            finally:
                slot.__exit__(None, None, None)

    def repository_runs(self, name):
        repo = self.project(name)["repo"].lower()
        names = {p["name"] for p in self.projects() if p["repo"].lower() == repo}
        return [r for r in self.runs() if r["project"] in names]

    @contextmanager
    def subscription(self, agent, cooldown=None):
        """Serialize calls and respect cooldowns; None does not record failures."""
        lock = self.file_lock(f"subscription-{agent}.lock")
        try:
            lock.__enter__()
        except TeamError as exc:
            raise CapacityWait(time.time() + 30) from exc
        try:
            key = f"quota-{agent}"
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if row and float(row[0]) > time.time():
                raise CapacityWait(float(row[0]))
            try:
                yield
            except QuotaError:
                if cooldown is not None:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                    (key, str(time.time() + cooldown)))
                    self.db.commit()
                raise
        finally:
            lock.__exit__(None, None, None)

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

    def update_project(self, name, remove=(), check=None, **changes):
        """Atomic read-modify-write; `check` may reject the result, which rolls back."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            project = self.project(name)
            for key in remove:
                project.pop(key, None)
            project.update(changes)
            if check:
                check(project)
            self.db.execute("UPDATE projects SET data=? WHERE name=?", (json.dumps(project), name))
        return project

    def pause(self, name, paused):
        # Short independent transaction: works while workers execute stages.
        return self.update_project(name, paused=paused)

    def register(self, name, repo, base, tests, **options):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
            raise TeamError("Project name must contain only letters, digits, underscore, or dash")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or ".." in repo:
            raise TeamError("Repository must be OWNER/REPO on github.com")
        if any(p["name"] == name for p in self.projects()):
            raise TeamError("Project name is already registered")
        if not tests:
            raise TeamError("At least one --test command is required")
        project = dict(name=name, repo=repo, base=base, tests=tests, paused=False,
                       ready_label="agent:ready", timeout=1800, max_revisions=2,
                       quota_cooldown=3600, max_quota_retries=3, codex_model=None, claude_model=None)
        project.update(options)
        configure_companions(repo, project.get("companions", []), project.get("companion_manifest"))
        self.save_project(project)
        return project

    def set_queue(self, name, numbers):
        numbers = list(numbers)
        if any(type(n) is not int or n < 1 for n in numbers):
            raise TeamError("Queue entries must be positive issue numbers")
        if len(numbers) != len(set(numbers)):
            raise TeamError("Duplicate queue entries are not allowed")
        self.update_project(name, queue_order=numbers)
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
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                            (run["id"], run["project"], run["issue"], json.dumps(run)))
            self.db.execute("INSERT INTO events(run,at,data) VALUES (?,?,?)",
                            (run["id"], time.time(), json.dumps(changes)))

    def create(self, project, issue, plan=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if issue["number"] is not None and any(r["issue"] == issue["number"] for r in self.repository_runs(project["name"])):
                raise TeamError("Issue already has a run in this repository")
            if issue["number"] is None and any(r["issue"] is None and
                    r["issue_digest"] == issue_fingerprint(issue)
                    for r in self.repository_runs(project["name"])):
                raise TeamError("This task scope already has a run; continue its history instead")
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
            if plan:
                run.update(plan)
            run["branch"] = f"agent-team/{run['issue'] if run['issue'] is not None else 'task'}-{run['id']}"
            if project.get("companions"):
                # Suite runs keep the primary basename so companions can sit beside it.
                run["checkout"] = basename(project["repo"])
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                            (run["id"], run["project"], run["issue"], json.dumps(run)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(index + 1)))
            return run

    def run_root(self, run):
        return self.home / "runs" / run["id"]

    def workspace(self, run):
        root = self.run_root(run) / "author"
        return root / run["checkout"] if run.get("checkout") else root

    def layout(self, run, name):
        """(root, checkout) for a fresh workspace; suite runs nest the checkout under its basename."""
        root = self.run_root(run) / name
        return root, (root / run["checkout"] if run.get("checkout") else root)

    def artifacts(self, run):
        path = self.home / "runs" / run["id"] / "artifacts"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
