import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_team import companions
from agent_team.cli import public_companions
from agent_team.coordinator import Coordinator
from agent_team.process import execute, git, TeamError
from agent_team.state import Store
from test_coordinator import FakeAgents, FakeGitHub


def commit(source, files, message="Commit"):
    for path, text in files.items():
        (source / path).write_text(text)
    git(source, "add", ".")
    git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-m", message)
    return git(source, "rev-parse", "HEAD")


class CompanionTests(unittest.TestCase):
    """Offline multi-repository fixtures: local bare repositories stand in for public GitHub ones."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        lib = self.source("lib")
        self.lib_v1 = commit(lib, {"lib.txt": "v1\n", ".gitignore": "build/\n"})
        self.lib_v2 = commit(lib, {"lib.txt": "v2\n"})
        self.published = {"example/lib": self.bare(lib, "lib")}
        self.store = Store(self.root / "state")
        self.github = FakeGitHub(None)
        self.agents = FakeAgents()
        self.team = Coordinator(self.store, self.github, self.agents)
        self.cloned = []

        def local_clone(repo, destination, base, timeout):
            return execute(["git", "clone", "--branch", base, str(self.remote), str(destination)], timeout=timeout)

        def local_git(cwd, *args):
            args = list(args)
            if "push" in args:
                args[args.index("push") + 1] = str(self.remote)
            return git(cwd, *args)

        def public_clone(repo, destination, timeout):
            # Only declared fixtures are "published"; anything else is unobtainable, like a private repository.
            self.cloned.append(repo)
            if repo not in self.published:
                raise TeamError(f"git failed (128): repository {repo} not found")
            execute(["git", "clone", "--no-checkout", str(self.published[repo]), str(destination)], timeout=timeout)

        self.patches = [patch("agent_team.coordinator.clone_repository", side_effect=local_clone),
                        patch("agent_team.coordinator.git", side_effect=local_git),
                        patch("agent_team.companions.clone", side_effect=public_clone)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.store.db.close()
        self.tmp.cleanup()

    def source(self, name):
        path = self.root / f"{name}-source"
        path.mkdir()
        execute(["git", "init", "-b", "main", str(path)])
        return path

    def bare(self, source, name):
        path = self.root / f"{name}.git"
        execute(["git", "clone", "--bare", str(source), str(path)])
        return path

    def primary(self, files, companion_list, manifest=None, test=None):
        source = self.source("demo")
        commit(source, {"README.md": "Fixture\n", **files})
        self.remote = self.bare(source, "demo")
        self.github.remote = self.remote
        # Passes only in a checkout named `demo` with the pinned companion beside it.
        test = test or 'test "$(basename "$PWD")" = demo && test -f ../lib/lib.txt && test -f feature.txt'
        self.project = self.store.register("demo", "example/demo", "main", [test],
                                           companions=companion_list,
                                           **({"companion_manifest": manifest} if manifest else {}))
        return self.project

    def tick(self, times=1):
        result = None
        for _ in range(times):
            result = self.team.tick("demo")
        return result

    def until(self, stage, limit=10):
        for _ in range(limit):
            run = self.tick()
            if run["stage"] == stage:
                return run
        self.fail(f"run did not reach {stage}: {run['stage']} {run.get('error')}")

    def test_manifest_pinned_suite_preserves_basenames_and_records_pins(self):
        manifest = {"companions": [{"repo": "example/lib", "rev": self.lib_v1}]}
        self.primary({"companions.json": json.dumps(manifest)}, [{"repo": "example/lib", "rev": None}],
                     "companions.json")
        run = self.until("ready")
        pins = [{"repo": "example/lib", "rev": self.lib_v1}]
        self.assertEqual(run["validated_companions"], pins)
        self.assertEqual(run["review_record"]["companions"], pins)
        run_root = self.store.run_root(run)
        author = self.store.workspace(run)
        self.assertEqual(author, run_root / "author" / "demo")
        self.assertEqual((author.parent / "lib" / "lib.txt").read_text(), "v1\n")
        for kind in ("validation", "review"):
            [root] = run_root.glob(f"{kind}-*")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["demo", "lib"])
            self.assertEqual(git(root / "lib", "rev-parse", "HEAD"), self.lib_v1)
        self.assertIn(f"../lib = example/lib at {self.lib_v1}", self.agents.prompts["implement"])
        self.assertIn(f"../lib = example/lib at {self.lib_v1}", self.agents.prompts["review"])
        self.assertIn(self.lib_v1, self.github.pull["body"])
        review = next(body for (_, marker), body in self.github.comments.items() if "-review-" in marker)
        self.assertIn(f"`example/lib` at `{self.lib_v1}`", review)
        self.assertIn(self.lib_v1, self.github.comments[(7, f"{run['id']}-ready")])
        self.assertEqual(set(self.cloned), {"example/lib"})

    def test_revision_reclones_author_companions(self):
        self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}])
        self.agents.reject = True
        self.assertEqual(self.tick(5)["stage"], "implement")
        run = self.until("ready")
        self.assertTrue(list(self.store.run_root(run).glob("companion-preserved-*-lib")))
        self.assertEqual(sorted(p.name for p in self.store.workspace(run).parent.iterdir()), ["demo", "lib"])

    def test_missing_pin_blocks_before_implementation(self):
        self.primary({}, [{"repo": "example/lib", "rev": None}])
        run = self.tick(2)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("Missing companion pin for example/lib", run["error"])
        self.assertEqual(self.agents.calls, [])

    def test_missing_manifest_blocks(self):
        self.primary({}, [{"repo": "example/lib", "rev": None}], "companions.json")
        run = self.tick(2)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("not committed", run["error"])
        self.assertEqual(self.agents.calls, [])

    def test_manifest_cannot_add_undeclared_companion(self):
        manifest = {"companions": [{"repo": "example/lib", "rev": self.lib_v1},
                                   {"repo": "example/private", "rev": self.lib_v1}]}
        self.primary({"companions.json": json.dumps(manifest)}, [{"repo": "example/lib", "rev": None}],
                     "companions.json")
        run = self.tick(2)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("undeclared companion example/private", run["error"])
        self.assertEqual(self.cloned, [])

    def test_unpublished_pin_blocks(self):
        self.primary({}, [{"repo": "example/lib", "rev": "0" * 40}])
        run = self.tick(2)
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("is not published", run["error"])

    def test_changed_pin_before_review_requires_new_validation(self):
        self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}])
        run = self.tick(4)
        self.assertEqual(run["stage"], "review")
        self.project["companions"][0]["rev"] = self.lib_v2
        self.store.save_project(self.project)
        run = self.tick()
        self.assertEqual(run["stage"], "validate")
        self.assertNotIn(("claude", "review"), self.agents.calls)
        self.assertEqual(self.github.statuses[-1], (run["sha"], "pending"))
        run = self.until("ready")
        self.assertEqual(run["validated_companions"], [{"repo": "example/lib", "rev": self.lib_v2}])
        self.assertEqual(run["review_record"]["companions"], run["validated_companions"])
        self.assertIn(f"example/lib at {self.lib_v2}", self.agents.prompts["review"])

    def test_changed_pin_invalidates_ready_evidence(self):
        self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}])
        run = self.until("ready")
        sha = run["sha"]
        self.project["companions"][0]["rev"] = self.lib_v2
        self.store.save_project(self.project)
        run = self.tick()
        self.assertNotEqual(run["stage"], "ready")
        self.assertIn((sha, "pending"), self.github.statuses)
        run = self.until("ready")
        self.assertEqual(run["sha"], sha)
        self.assertEqual(run["validated_companions"][0]["rev"], self.lib_v2)
        self.assertEqual([role for _, role in self.agents.calls], ["implement", "review", "review"])
        # Both reviews of the same commit remain published as separate evidence.
        reviews = [body for (_, marker), body in self.github.comments.items() if "-review-" in marker]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(any(self.lib_v1 in body for body in reviews))
        self.assertTrue(any(self.lib_v2 in body for body in reviews))
        self.assertIn(self.lib_v2, self.github.comments[(7, f"{run['id']}-ready")])

    def test_validation_cannot_change_companions(self):
        for name, command in (("contents", "echo changed > ../lib/lib.txt"),
                              ("added file", "echo extra > ../lib/extra.txt"),
                              ("assume-unchanged edit", "git -C ../lib update-index --assume-unchanged lib.txt"
                                                        " && echo changed > ../lib/lib.txt"),
                              ("skip-worktree edit", "git -C ../lib update-index --skip-worktree lib.txt"
                                                     " && echo changed > ../lib/lib.txt"),
                              ("ignored artifact", "mkdir ../lib/build && echo dep > ../lib/build/dep.txt"),
                              ("self-ignored directory", "mkdir ../lib/vendor && printf '*\\n' > ../lib/vendor/.gitignore"
                                                         " && echo dep > ../lib/vendor/dep.txt"),
                              ("HEAD", "git -C ../lib checkout -q --detach HEAD~1"),
                              ("configuration", "git -C ../lib config user.name Mallory"),
                              ("failing command", "echo changed > ../lib/lib.txt; false")):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                self.primary({}, [{"repo": "example/lib", "rev": self.lib_v2}], test=command)
                run = self.tick(3)
                self.assertEqual((run["stage"], run["resume_stage"]), ("blocked", "validate"))
                self.assertIn("Companion example/lib changed", run["error"])
                self.assertIsNone(run.get("validated_sha"))
                self.assertEqual(run.get("revision_history", []), [])
                self.assertEqual(self.github.creates, 0)

    @staticmethod
    def hide_edit(lib):
        git(lib, "update-index", "--assume-unchanged", "lib.txt")
        (lib / "lib.txt").write_text("changed\n")

    @staticmethod
    def add_ignored(lib):
        (lib / "build").mkdir()
        (lib / "build" / "dep.txt").write_text("dep\n")

    def test_review_cannot_change_companions(self):
        for name, mutate in (("contents", lambda lib: (lib / "lib.txt").write_text("changed\n")),
                             ("assume-unchanged edit", self.hide_edit),
                             ("ignored artifact", self.add_ignored)):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}])
                self.assertEqual(self.tick(4)["stage"], "review")
                original = self.agents.run

                def mutating(agent, role, prompt, cwd, artifacts, project):
                    if role == "review":
                        mutate(Path(cwd).parent / "lib")
                    return original(agent, role, prompt, cwd, artifacts, project)

                with patch.object(self.agents, "run", side_effect=mutating):
                    run = self.tick()
                self.assertEqual((run["stage"], run["resume_stage"]), ("blocked", "review"))
                self.assertIn("Companion example/lib changed", run["error"])
                self.assertIsNone(run.get("review_record"))
                self.assertFalse([m for _, m in self.github.comments if "-review-" in m])

    def test_verify_does_not_trust_git_status(self):
        pins = [{"repo": "example/lib", "rev": self.lib_v1}]
        for name, mutate in (("assume-unchanged edit", self.hide_edit), ("ignored artifact", self.add_ignored)):
            with self.subTest(name):
                root = self.root / name.replace(" ", "-")
                root.mkdir()
                baselines = companions.populate(root, pins, 60)
                companions.verify(root, pins, baselines)
                mutate(root / "lib")
                # Git reports a clean pinned checkout, yet the files differ from the pin.
                self.assertEqual(git(root / "lib", "status", "--porcelain"), "")
                self.assertEqual(git(root / "lib", "rev-parse", "HEAD"), self.lib_v1)
                with self.assertRaisesRegex(TeamError, "Companion example/lib changed"):
                    companions.verify(root, pins, baselines)

    def interrupt_review(self, reject, max_revisions=None):
        """A verdict with the v1 pin is saved, then the process stops before it is recorded."""
        self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}])
        if max_revisions is not None:
            self.project["max_revisions"] = max_revisions
            self.store.save_project(self.project)
        self.agents.reject = reject
        self.assertEqual(self.tick(4)["stage"], "review")
        with patch.object(self.team, "record_review", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tick()
        self.tick()
        run = self.store.runs()[0]
        self.assertEqual((run["stage"], run["resume_stage"], run["review_sha"]), ("blocked", "review", run["sha"]))
        self.project["companions"][0]["rev"] = self.lib_v2
        self.store.save_project(self.project)
        return run

    def assert_superseded(self, run, verdict, sha):
        self.assertNotIn(sha, run.get("rejected_shas", []))
        self.assertEqual(run.get("revision_history", []), [])
        [old] = run["superseded_evidence"]
        self.assertEqual((old["kind"], old["sha"], old["record"]["report"]["verdict"]), ("review", sha, verdict))
        self.assertEqual(old["companions"], [{"repo": "example/lib", "rev": self.lib_v1}])
        self.assertEqual(run["validated_companions"], [{"repo": "example/lib", "rev": self.lib_v2}])
        self.assertEqual(run["review_record"]["companions"], run["validated_companions"])
        self.assertEqual(run["review_record"]["report"]["verdict"], "pass")

    def test_interrupted_review_with_changed_pins_is_superseded_on_resume(self):
        for reject, verdict in ((True, "changes_requested"), (False, "pass")):
            with self.subTest(verdict):
                self.tearDown()
                self.setUp()
                run = self.interrupt_review(reject)
                sha = run["sha"]
                self.command("resume", run["id"])
                run = self.tick()
                self.assertEqual(run["stage"], "validate")
                self.assertIsNone(run["review_record"])
                run = self.until("ready")
                self.assertEqual((run["sha"], run["round"]), (sha, 0))
                self.assert_superseded(run, verdict, sha)
                self.assertEqual([role for _, role in self.agents.calls], ["implement", "review", "review"])
                # The superseded verdict was never published as the outcome.
                reviews = [body for (_, marker), body in self.github.comments.items() if "-review-" in marker]
                self.assertEqual(len(reviews), 1)
                self.assertIn(self.lib_v2, reviews[0])

    def test_refresh_after_interrupted_review_with_changed_pins_does_not_record_it(self):
        for reject, verdict in ((True, "changes_requested"), (False, "pass")):
            with self.subTest(verdict):
                self.tearDown()
                self.setUp()
                # With no revisions left, recording the old rejection would hand off to the operator.
                run = self.interrupt_review(reject, max_revisions=0)
                sha = run["sha"]
                self.team.refresh(run["id"])
                run = self.store.get(run["id"])
                self.assertEqual(run["stage"], "validate")
                self.assertNotIn(sha, run.get("rejected_shas", []))
                self.assertEqual(run["superseded_evidence"][0]["record"]["report"]["verdict"], verdict)
                self.assertNotIn("handoffs", run)
                run = self.until("ready")
                self.assert_superseded(run, verdict, sha)

    def test_interrupted_validation_failure_with_changed_pins_is_superseded(self):
        self.primary({}, [{"repo": "example/lib", "rev": self.lib_v1}], test='test "$(cat ../lib/lib.txt)" = v2')
        with patch.object(self.team, "record_validation_failure", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(3)
        self.tick()
        run = self.store.runs()[0]
        self.assertEqual((run["stage"], run["resume_stage"]), ("blocked", "validate"))
        self.project["companions"][0]["rev"] = self.lib_v2
        self.store.save_project(self.project)
        self.command("resume", run["id"])
        run = self.until("ready")
        self.assertEqual(run.get("revision_history", []), [])
        self.assertNotIn(run["sha"], run.get("rejected_shas", []))
        [old] = run["superseded_evidence"]
        self.assertEqual((old["kind"], old["tests"][0]["exit_code"]), ("validation", 1))
        self.assertEqual(old["companions"], [{"repo": "example/lib", "rev": self.lib_v1}])

    def command(self, *args):
        from agent_team.cli import dispatch, parser
        with patch("agent_team.cli.Coordinator", return_value=self.team), patch("agent_team.cli.GitHub"), \
                patch("agent_team.cli.emit"):
            return dispatch(parser().parse_args(list(args)), self.store)

    def test_removed_manifest_pin_blocks_ready_run(self):
        manifest = {"companions": [{"repo": "example/lib", "rev": self.lib_v1}]}
        self.primary({"companions.json": json.dumps(manifest)}, [{"repo": "example/lib", "rev": None}],
                     "companions.json")
        self.until("ready")
        self.project["companion_manifest"] = "moved.json"
        self.store.save_project(self.project)
        run = self.tick()
        self.assertEqual(run["stage"], "blocked")
        self.assertIn("not committed", run["error"])

    def test_configuration_is_validated(self):
        for companion_list, manifest in (([{"repo": "example/lib", "rev": "main"}], None),
                                         ([{"repo": "other/demo", "rev": None}], None),
                                         ([{"repo": "a/lib", "rev": None}, {"repo": "b/LIB", "rev": None}], None),
                                         ([{"repo": "../lib", "rev": None}], None),
                                         ([], "companions.json"),
                                         ([{"repo": "example/lib", "rev": None}], "../companions.json"),
                                         ([{"repo": "example/lib", "rev": None}], "/companions.json")):
            with self.assertRaises(TeamError):
                self.store.register("bad", "example/demo", "main", ["true"], companions=companion_list,
                                    **({"companion_manifest": manifest} if manifest else {}))
        self.assertEqual(self.store.projects(), [])
        with self.assertRaises(TeamError):
            companions.parse(["example/lib@v1.0"])
        self.assertEqual(companions.parse(["example/lib", f"example/lib@{self.lib_v1}"]),
                         [{"repo": "example/lib", "rev": None}, {"repo": "example/lib", "rev": self.lib_v1}])

    def test_single_repository_registration_is_unchanged(self):
        project = self.store.register("solo", "example/solo", "main", ["true"])
        self.assertNotIn("companions", project)
        self.assertNotIn("companion_manifest", project)
        run = self.store.create(project, {"number": 1, "title": "Solo"})
        self.assertNotIn("checkout", run)
        self.assertEqual(self.store.workspace(run), self.store.home / "runs" / run["id"] / "author")
        self.assertFalse(self.team.pins_changed(project, run))

    def test_companions_are_public_and_anonymous(self):
        class Repos:
            def repo(self, name):
                return {"full_name": name, "private": name.endswith("secret"),
                        "visibility": "private" if name.endswith("secret") else "public"}

        self.assertEqual(public_companions(Repos(), ["example/lib"]), [{"repo": "example/lib", "rev": None}])
        with self.assertRaises(TeamError):
            public_companions(Repos(), ["example/secret"])
        self.patches[2].stop()
        try:
            with patch("agent_team.companions.execute") as run:
                companions.clone("example/lib", self.root / "lib", 60)
            args = run.call_args.args[0]
            self.assertIn("credential.helper=", args)
            self.assertNotIn("!gh auth git-credential", " ".join(args))
            self.assertIn("https://github.com/example/lib.git", args)
            self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])
        finally:
            self.patches[2].start()


if __name__ == "__main__":
    unittest.main()
