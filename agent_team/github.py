"""GitHub operations owned by the coordinator, never an agent's response text."""
import json
from urllib.parse import quote, urlencode

from .process import execute, TeamError
from .state import issue_fingerprint

# GitHub rejects comment bodies above 65,536 characters; leave room for markers and headings.
PART = 60000


def split_body(body, size=PART):
    """Split text into ordered parts, preferring line breaks. Nothing is dropped."""
    parts = []
    while len(body) > size:
        cut = body.rfind("\n", size // 2, size)
        cut = cut + 1 if cut > 0 else size
        parts.append(body[:cut])
        body = body[cut:]
    return parts + [body]


class GitHub:
    def __init__(self):
        self._login = None

    def api(self, endpoint, method="GET", data=None):
        args = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if data is not None:
            args += ["--input", "-"]
        output = execute(args, input=json.dumps(data) if data is not None else None).stdout
        return json.loads(output) if output.strip() else None

    def pages(self, endpoint):
        args = ["gh", "api", "--hostname", "github.com", "--paginate", "--slurp", endpoint]
        return [item for page in json.loads(execute(args).stdout) for item in page]

    def login(self):
        if self._login is None:
            # GraphQL viewer also represents installation-token app identities;
            # REST /user is only available to user authentication.
            result = self.api("graphql", "POST", {"query": "query { viewer { login } }"})
            if result.get("errors") or not result.get("data", {}).get("viewer"):
                raise TeamError("Cannot determine the authenticated GitHub identity")
            self._login = result["data"]["viewer"]["login"]
        return self._login

    def repo(self, repo):
        return self.api(f"repos/{repo}")

    def issues(self, project, *, ready=True):
        query = {"state": "open", "per_page": 100, "sort": "created", "direction": "asc"}
        if ready:
            query["labels"] = project["ready_label"]
        return [i for i in self.pages(f"repos/{project['repo']}/issues?{urlencode(query)}")
                if "pull_request" not in i]

    def issue(self, repo, number):
        return self.api(f"repos/{repo}/issues/{number}")

    def approve(self, project, number):
        issue = self.issue(project["repo"], number)
        if issue["state"] != "open" or "pull_request" in issue:
            raise TeamError("Only open issues can be approved")
        self.setup(project)
        self.comment(project["repo"], number, f"approval-{number}",
                     f"Approved for Agent Team implementation.\n\nIssue content SHA-256: `{issue_fingerprint(issue)}`\n\n"
                     "Changing the title or body invalidates this approval.")
        self.api(f"repos/{project['repo']}/issues/{number}/labels", "POST", {"labels": [project["ready_label"]]})
        return {"issue": number, "digest": issue_fingerprint(issue)}

    def authorized(self, project, issue):
        tag = f"<!-- agent-team:approval-{issue['number']} -->"
        fingerprint = f"Issue content SHA-256: `{issue_fingerprint(issue)}`"
        comments = self.pages(f"repos/{project['repo']}/issues/{issue['number']}/comments?per_page=100")
        return any(tag in c["body"] and fingerprint in c["body"] and
                   c["user"]["login"] == self.login() for c in comments)

    def setup(self, project):
        existing = {l["name"] for l in self.pages(f"repos/{project['repo']}/labels?per_page=100")}
        for name, color in [(project["ready_label"], "0E8A16"), ("agent:discovered", "D4C5F9")]:
            if name not in existing:
                self.api(f"repos/{project['repo']}/labels", "POST", {"name": name, "color": color})

    def comment(self, repo, number, marker, body, heading=None):
        """Create or update a marked comment. Long bodies continue in marked follow-up
        comments (`heading` names what they continue) instead of being truncated."""
        endpoint = f"repos/{repo}/issues/{number}/comments"
        comments = self.pages(endpoint + "?per_page=100")
        mine = [c for c in comments if c["user"]["login"] == self.login()]

        def existing(tag):
            return next((c for c in mine if tag in c["body"]), None)

        parts = split_body(body)
        for index, text in enumerate(parts):
            tag = f"<!-- agent-team:{marker} -->" if index == 0 else f"<!-- agent-team:{marker}-part-{index + 1} -->"
            if index:
                text = f"**{heading or 'Agent Team'} (continued, part {index + 1} of {len(parts)})**\n\n" + text
            if found := existing(tag):
                self.api(f"repos/{repo}/issues/comments/{found['id']}", "PATCH", {"body": tag + "\n" + text})
            else:
                self.api(endpoint, "POST", {"body": tag + "\n" + text})
        # Retire continuation parts left over from a longer earlier version.
        index = len(parts) + 1
        while found := existing(tag := f"<!-- agent-team:{marker}-part-{index} -->"):
            self.api(f"repos/{repo}/issues/comments/{found['id']}", "PATCH",
                     {"body": tag + "\n*No longer used; the updated text is in the comments above.*"})
            index += 1

    def find_pr(self, project, branch):
        query = urlencode({"state": "all", "head": project["repo"].split('/')[0] + ':' + branch})
        pulls = self.pages(f"repos/{project['repo']}/pulls?{query}&per_page=100")
        if len(pulls) > 1:
            raise TeamError("Multiple PRs for the run branch; manual reconciliation required")
        return pulls[0] if pulls else None

    def create_pr(self, project, run, body):
        found = self.find_pr(project, run["branch"])
        if found:
            # Republication refreshes the description so it matches the current validation evidence.
            if found.get("body") != body:
                found = self.api(f"repos/{project['repo']}/pulls/{found['number']}", "PATCH", {"body": body})
            return found
        return self.api(f"repos/{project['repo']}/pulls", "POST", {
            "title": run["title"][:240], "head": run["branch"], "base": project["base"],
            "body": body, "draft": True,
        })

    def pr(self, repo, number):
        pr = self.api(f"repos/{repo}/pulls/{number}")
        live = pr["base"]["sha"]
        if pr.get("state") == "open" and not pr.get("merged"):
            live = self.api(f"repos/{repo}/git/ref/heads/{quote(pr['base']['ref'])}")["object"]["sha"]
        pr["base"] = dict(pr["base"], snapshot_sha=pr["base"]["sha"], sha=live)
        return pr

    def push_access(self, project, pr):
        head = (pr["head"].get("repo") or {}).get("full_name")
        if not head:
            return {"allowed": False, "reason": "the head repository is unavailable"}
        try:
            if (self.repo(head).get("permissions") or {}).get("push"):
                return {"allowed": True, "reason": f"write access to {head}"}
            if head.casefold() == project["repo"].casefold():
                return {"allowed": False, "reason": f"no write access to {head}"}
            if pr.get("maintainer_can_modify") and (self.repo(project["repo"]).get("permissions") or {}).get("push"):
                return {"allowed": True, "reason": f"maintainer edits allowed on fork {head}"}
        except TeamError as error:
            return {"allowed": False, "reason": f"write access to {head} could not be verified ({error})"}
        return {"allowed": False, "reason": f"no write access to fork {head} and maintainer edits are not available"}

    def mark_ready(self, repo, number):
        execute(["gh", "pr", "ready", str(number), "--repo", repo])

    def status(self, repo, sha, state, description):
        self.api(f"repos/{repo}/statuses/{sha}", "POST", {
            "state": state, "context": "agent-team/review", "description": description[:140],
        })

    def ci(self, repo, sha):
        checks = self.api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        statuses = self.api(f"repos/{repo}/commits/{sha}/status?per_page=100")
        # Avoid silently ignoring checks beyond our inspected window.
        if checks["total_count"] > 100 or statuses["total_count"] > 100:
            raise TeamError("More than 100 CI results; inspect manually")
        values = []
        for check in checks["check_runs"]:
            if check["status"] != "completed":
                values.append("pending")
            else:
                values.append("success" if check["conclusion"] in {"success", "neutral", "skipped"} else "failure")
        values += [s["state"] for s in statuses["statuses"] if s["context"] != "agent-team/review"]
        if any(v in {"failure", "error"} for v in values):
            return "failure"
        return "pending" if "pending" in values else "success"

    def create_issue(self, project, title, body):
        return self.api(f"repos/{project['repo']}/issues", "POST", {
            "title": title[:240], "body": body, "labels": ["agent:discovered"],
        })
