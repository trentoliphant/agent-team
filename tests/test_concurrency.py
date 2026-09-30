"""Offline concurrency regressions. Each thread owns its SQLite connection."""
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import select
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_team.coordinator import Coordinator
from agent_team.cli import dispatch, parser
from agent_team.process import QuotaError, TeamError
from agent_team.state import CapacityWait, CoordinatorBusy, Store


class GitHub:
    def issue(self, repo, number):
        return dict(number=number, title='Work', body='', state='open',
                    labels=[{'name': 'agent:ready'}])

    def authorized(self, project, issue):
        return True

    def comment(self, *args, **kwargs):
        pass


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        for name, repo in [('one', 'owner/one'), ('two', 'owner/two'),
                           ('alias', 'OWNER/ONE'), ('three', 'owner/three')]:
            self.store.register(name, repo, 'main', ['true'])

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_default_serial_limit_and_alias_exclusion(self):
        other = Store(self.tmp.name)
        try:
            with self.store.worker('one'):
                for name in ('one', 'alias', 'two'):
                    with self.assertRaises(TeamError), other.worker(name):
                        pass
            self.store.set_concurrency(2)
            with self.store.worker('one'), other.worker('two'):
                with self.assertRaises(TeamError), self.store.worker('three'):
                    pass
                with self.assertRaises(TeamError), self.store.worker('alias'):
                    pass
                with self.assertRaises(TeamError), other.lock():
                    pass
                other.pause('one', True)
                self.assertTrue(self.store.project('one')['paused'])
        finally:
            other.db.close()

    def test_ticks_overlap_and_rotation_and_journals_survive(self):
        self.store.set_concurrency(2)
        barrier = threading.Barrier(2)

        def advance(name):
            store = Store(self.tmp.name)
            team = Coordinator(store, GitHub())

            def prepare(project, run):
                barrier.wait(timeout=10)
                for index in range(20):
                    store.save(run, progress=index)
                store.save(run, stage='implement')

            try:
                with patch.object(team, 'prepare', side_effect=prepare):
                    return team.tick(name, 1)
            finally:
                store.db.close()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(advance, ('one', 'two')))
        self.assertEqual({r['author'] for r in results}, {'codex', 'claude'})
        for run in results:
            self.assertEqual(self.store.get(run['id'])['progress'], 19)
            self.assertFalse(self.store.get(run['id'])['in_flight'])
            self.assertNotEqual(run['author'], run['reviewer'])
        self.assertEqual(self.store.db.execute(
            'SELECT count(*) FROM events WHERE data LIKE ?', ('%progress%',)).fetchone()[0], 40)

    def test_duplicate_claim_is_atomic_across_aliases(self):
        barrier = threading.Barrier(2)

        def claim(name):
            store = Store(self.tmp.name)
            try:
                barrier.wait(timeout=10)
                try:
                    return store.create(store.project(name), GitHub().issue('', 1))
                except TeamError:
                    return None
            finally:
                store.db.close()

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(claim, ('one', 'alias')))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.store.runs()), 1)
        self.assertEqual(self.store.db.execute(
            "SELECT value FROM meta WHERE key='author_rotation'").fetchone()[0], '1')

    def test_live_worker_is_not_recovered_and_interruption_is_explicit(self):
        self.store.set_concurrency(2)
        run = self.store.create(self.store.project('one'), GitHub().issue('', 1))
        self.store.save(run, in_flight=True)
        other = Store(self.tmp.name)
        try:
            with self.store.worker('one'):
                with self.assertRaises(TeamError):
                    Coordinator(other, GitHub()).tick('one', 1)
                self.assertTrue(other.get(run['id'])['in_flight'])
            team = Coordinator(other, GitHub())
            with patch.object(team, 'prepare') as stage:
                self.assertEqual(team.tick('one', 1)['stage'], 'waiting')
                team.tick('one', 1)
                stage.assert_not_called()
            recovered = other.get(run['id'])
            self.assertEqual(recovered['resume_stage'], 'prepare')
            self.assertEqual(team.tick('alias', 1)['stage'], 'waiting')
            with patch.object(team, 'prepare', side_effect=lambda p, r: other.save(r, stage='implement')):
                self.assertEqual(team.tick('two', 1)['stage'], 'implement')
        finally:
            other.db.close()

    def test_handoff_and_repair_exclude_alias_intake_but_not_other_repositories(self):
        self.store.set_concurrency(2)
        run = self.store.create(self.store.project('one'), GitHub().issue('', 1))
        team = Coordinator(self.store, GitHub())
        for stage in ('handoff', 'repair'):
            with self.subTest(stage=stage):
                self.store.save(run, stage=stage, in_flight=True)
                with patch.object(self.store, 'create', wraps=self.store.create) as create:
                    self.assertEqual(team.tick('alias', 2)['stage'], 'waiting')
                    self.assertEqual(team.tick('alias')['stage'], 'waiting')
                    create.assert_not_called()
                # A saved operator recovery state survives interruption reconciliation.
                self.assertEqual(team.tick('one', 1)['stage'], stage)
                persisted = self.store.get(run['id'])
                self.assertEqual(persisted['stage'], stage)
                self.assertFalse(persisted['in_flight'])
                # Repeated targeted polls report recovery without invoking either agent.
                with patch.object(team.agents, 'run') as call:
                    self.assertEqual(team.tick('one', 1)['stage'], stage)
                    self.assertEqual(team.tick('one')['stage'], 'waiting')
                    call.assert_not_called()
                self.assertEqual(self.store.get(run['id'])['stage'], stage)
                with patch.object(team.github, 'issues', return_value=[], create=True):
                    self.assertEqual(team.queue('alias')['recovery_runs'], [run['id']])
                with patch.object(team, 'prepare', side_effect=lambda p, r: self.store.save(r, stage='closed')):
                    self.assertEqual(team.tick('two', 2 if stage == 'handoff' else 3)['stage'], 'closed')
                self.assertEqual(len(self.store.repository_runs('alias')), 1)

    def test_shared_quota_and_family_capacity_do_not_repeat_calls(self):
        other = Store(self.tmp.name)
        try:
            with self.store.subscription('codex', 60):
                with self.assertRaises(CapacityWait), other.subscription('codex', 60):
                    self.fail('Busy subscription was reused')
                with other.subscription('claude', 60):
                    pass
            with self.assertRaises(QuotaError), self.store.subscription('codex', 60):
                raise QuotaError('quota')
            with self.assertRaises(CapacityWait), other.subscription('codex', 60):
                self.fail('Cooling subscription was reused')
            run = self.store.create(self.store.project('one'), GitHub().issue('', 1))
            self.store.save(run, stage='prepare', quota_attempts=2)
            team = Coordinator(self.store, GitHub())
            with patch.object(team, 'prepare', side_effect=lambda p, r: team.call_agent(
                    'codex', 'implement', '', None, None, p)), patch.object(team.agents, 'run') as call:
                result = team.tick('one', 1)
                self.assertEqual(result['stage'], 'quota_wait')
                self.assertEqual(result['quota_attempts'], 2)
                call.assert_not_called()
        finally:
            other.db.close()

    def test_interrupt_releases_locks_but_requires_operator_resume(self):
        team = Coordinator(self.store, GitHub())
        with patch.object(team, 'prepare', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                team.tick('one', 1)
        run = self.store.runs('one')[0]
        self.assertTrue(run['in_flight'])
        with patch.object(team, 'prepare') as stage:
            team.tick('one', 1)
            stage.assert_not_called()
        self.assertEqual(self.store.get(run['id'])['stage'], 'blocked')
        with self.store.lock(), patch('agent_team.cli.emit'):
            dispatch(parser().parse_args(['resume', run['id']]), self.store)
        with patch.object(team, 'prepare', side_effect=lambda p, r: self.store.save(r, stage='implement')):
            self.assertEqual(team.tick('one', 1)['stage'], 'implement')

    def test_dead_process_releases_repository_and_capacity_locks(self):
        script = (
            "import sys, time\n"
            "from agent_team.state import Store\n"
            "s = Store(sys.argv[1])\n"
            "with s.worker('one'):\n"
            " r = s.create(s.project('one'), {'number': 1, 'title': 'Work'})\n"
            " s.save(r, in_flight=True)\n"
            " print('locked', flush=True)\n"
            " time.sleep(30)\n"
        )
        child = subprocess.Popen([sys.executable, '-c', script, self.tmp.name],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'Worker did not acquire its lock')
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            with self.assertRaises(TeamError), self.store.worker('alias'):
                pass
            child.terminate()
            child.wait(timeout=5)
            with self.store.worker('alias'):
                pass
            team = Coordinator(self.store, GitHub())
            with patch.object(team, 'prepare') as stage:
                team.tick('one', 1)
                stage.assert_not_called()
            self.assertEqual(self.store.runs('one')[0]['stage'], 'blocked')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()

    def test_configuration_and_pause_changes_are_independent(self):
        with self.store.lock(), patch('agent_team.cli.emit'):
            dispatch(parser().parse_args(['configure', '--concurrency', '3']), self.store)
        self.assertEqual(self.store.concurrency(), 3)
        for invalid in (0, -1, True):
            with self.assertRaises(TeamError):
                self.store.set_concurrency(invalid)
        other = Store(self.tmp.name)
        try:
            other.pause('one', True)
            self.store.update_project('one', timeout=42)
            self.store.set_queue('one', [2, 1])
            self.assertTrue(other.project('one')['paused'])
            self.assertEqual(other.project('one')['timeout'], 42)
            self.assertEqual(other.project('one')['queue_order'], [2, 1])
        finally:
            other.db.close()

    def test_targeted_watch_polls_busy_workers_without_recovery(self):
        args = parser().parse_args(['run', 'one', '--issue', '1', '--watch'])
        with patch('agent_team.cli.Coordinator') as coordinator, \
                patch('agent_team.cli.time.sleep') as sleep, patch('agent_team.cli.emit') as emit:
            coordinator.return_value.tick.side_effect = [
                CoordinatorBusy('Repository already has a live coordinator worker'),
                {'project': 'one', 'stage': 'paused'},
            ]
            dispatch(args, self.store)
            self.assertEqual(coordinator.return_value.tick.call_count, 2)
            sleep.assert_called_once_with(30)
            self.assertEqual(emit.call_args_list[0].args[0]['stage'], 'busy')
            self.assertEqual(self.store.runs(), [])

    def test_pending_github_write_reconciles_while_another_repo_is_live(self):
        self.store.set_concurrency(2)
        run = self.store.create(self.store.project('one'), GitHub().issue('', 1))
        self.store.save(run, stage='publish', pr=7, sha='new', published_sha='old',
                        pending_push_sha='new', base_sha='base')
        github = GitHub()
        github.pr = lambda *args: dict(state='open', merged=False,
                                      head={'sha': 'new'}, base={'sha': 'base', 'ref': 'main'})
        github.status = lambda *args: self.fail('Reconciled push was treated as external')
        with self.store.worker('two'), self.store.worker('one'):
            team = Coordinator(self.store, github)
            self.assertTrue(team.reconcile(self.store.project('one'), run))
            self.assertTrue(team.reconcile(self.store.project('one'), run))
        persisted = self.store.get(run['id'])
        self.assertEqual(persisted['published_sha'], 'new')
        self.assertIsNone(persisted['pending_push_sha'])
