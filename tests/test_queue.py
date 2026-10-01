"""Model-free queue, authorization, and targeted watch regressions."""
import tempfile
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from agent_team.cli import dispatch, parser
from agent_team.coordinator import Coordinator
from agent_team.process import TeamError
from agent_team.state import Store


class Issues:
    def __init__(self):
        self.items = {n: dict(number=n, title=str(n), body='', state='open',
                             created_at=f'2026-01-{n:02d}',
                             labels=[{'name': 'agent:ready'}]) for n in (1, 2, 3)}

    def issues(self, project, ready=True):
        return [i for i in self.items.values() if i['state'] == 'open']

    def issue(self, repo, number):
        if number not in self.items:
            raise TeamError('Missing issue')
        return self.items[number]

    def authorized(self, project, issue):
        return issue.get('approved', True)

    def comment(self, *args):
        pass


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.store.register('demo', 'owner/repo', 'main', ['true'])
        self.github = Issues()
        self.team = Coordinator(self.store, self.github)
        self.stage = patch.object(self.team, 'prepare', side_effect=lambda p, r: self.store.save(r, stage='implement'))
        self.stage.start()

    def tearDown(self):
        self.stage.stop()
        self.store.db.close()
        self.temp.cleanup()

    def test_partial_selection_and_tracked_watch_arguments(self):
        args = parser().parse_args(["select", "demo", "--task", "Scoped task", "--operations",
                                    "implement", "validate", "--grant", "edit", "--plan"])
        self.assertEqual(args.operations, ["implement", "validate"])
        self.assertEqual(args.grant, ["edit"])
        self.assertTrue(args.plan)
        args = parser().parse_args(["select", "demo", "--run", "tracked", "--operations", "checks"])
        self.assertEqual(args.operations, ["checks"])
        self.assertEqual(args.grant, [])
        args = parser().parse_args(["run", "demo", "--run", "tracked", "--watch"])
        self.assertEqual(args.run_id, "tracked")
        self.assertTrue(args.watch)
        args = parser().parse_args(["continue", "tracked", "--operations", "validate",
                                    "--contributor", "human"])
        self.assertEqual(args.contributor, ["human"])
        args = parser().parse_args(["refresh", "tracked", "--grant", "edit", "--contributor", "human"])
        self.assertEqual(args.grant, "edit")

    def test_persistence_precedence_and_clear(self):
        self.store.set_queue('demo', [3, 2])
        other = Store(self.temp.name)
        self.assertEqual(other.project('demo')['queue_order'], [3, 2])
        other.db.close()
        self.assertEqual(self.team.queue('demo')['effective_queue'], [3, 2, 1])
        run = self.team.tick('demo', 2)
        self.assertEqual(run['issue'], 2)
        self.assertEqual(self.store.project('demo')['queue_order'], [3, 2])
        self.store.save(run, stage='closed')
        self.assertEqual(self.team.tick('demo')['issue'], 3)
        self.store.set_queue('demo', [])
        self.assertEqual(self.team.queue('demo')['effective_queue'], [1])

    def test_queue_retains_all_task_runs_in_both_row_orders(self):
        project = self.store.project('demo')
        stopped = self.store.create(project, dict(number=None, title='Stopped task', body='Scope one'))
        self.store.save(stopped, stage='stopped')
        active = self.store.create(project, dict(number=None, title='Active task', body='Scope two'))
        issue = self.store.create(project, self.github.items[2])
        for runs in ([stopped, active, issue], [issue, active, stopped]):
            with self.subTest(order=[r['id'] for r in runs]), \
                    patch.object(self.store, 'repository_runs', return_value=runs):
                view = self.team.queue('demo')
            self.assertCountEqual(view['active_runs'], [active['id'], issue['id']])
            self.assertEqual(view['recovery_runs'], [stopped['id']])
            self.assertEqual(view['effective_queue'], [1, 3])
            entry = next(e for e in view['entries'] if e['issue'] == 2)
            self.assertEqual(entry['reason'], 'existing run: prepare')

    def test_status_text_identifies_task_and_issue_runs(self):
        project = self.store.project('demo')
        task = self.store.create(project, dict(number=None, title='Task', body='Explicit scope'))
        issue = self.store.create(project, self.github.items[2])
        output = io.StringIO()
        with redirect_stdout(output):
            dispatch(parser().parse_args(['status', '--project', 'demo']), self.store)
        self.assertIn(f"{task['id']}  demo  task  prepare", output.getvalue())
        self.assertIn(f"{issue['id']}  demo  #2  prepare", output.getvalue())
        self.assertNotIn('#None', output.getvalue())

    def test_invalid_order_is_atomic(self):
        self.store.set_queue('demo', [2])
        for order in ([2, 2], [0], [-1], ['1']):
            with self.assertRaises(TeamError):
                self.store.set_queue('demo', order)
            self.assertEqual(self.store.project('demo')['queue_order'], [2])

    def test_ineligible_entries_explained_and_skipped(self):
        self.store.set_queue('demo', [9, 3, 2])
        self.github.items[3]['state'] = 'closed'
        self.github.items[2]['approved'] = False
        view = self.team.queue('demo')
        self.assertEqual(view['effective_queue'], [1])
        self.assertTrue(all(e['reason'] for e in view['entries'][:3]))
        self.assertEqual(self.team.tick('demo')['issue'], 1)

    def test_invalid_selection_never_falls_back(self):
        for number, field, value in ((2, 'approved', False), (2, 'labels', []),
                                     (2, 'state', 'closed'), (2, 'pull_request', {})):
            saved = dict(self.github.items[2])
            self.github.items[2][field] = value
            with self.assertRaises(TeamError):
                self.team.tick('demo', number)
            self.assertEqual(self.store.runs(), [])
            self.github.items[2] = saved
        for number in (0, -1, 9):
            with self.assertRaises(TeamError):
                self.team.tick('demo', number)
        self.assertEqual(self.store.runs(), [])

    def test_existing_run_conflicts_and_recovery(self):
        run = self.team.tick('demo', 2)
        with self.assertRaises(TeamError):
            self.team.tick('demo', 1)
        for stage in ('blocked', 'quota_wait', 'handoff', 'repair'):
            self.store.save(run, stage=stage, retry_at=10**20)
            with self.assertRaises(TeamError):
                self.team.tick('demo', 1)
            self.assertEqual(self.team.tick('demo')['stage'], 'waiting')
        self.store.save(run, stage='prepare')
        self.assertEqual(self.team.tick('demo', 2)['id'], run['id'])
        self.assertEqual(len(self.store.runs()), 1)
        self.store.save(run, stage='closed')
        with self.assertRaises(TeamError):
            self.team.tick('demo', 2)

    def test_interruption_requires_resume(self):
        run = self.team.tick('demo', 2)
        self.store.save(run, in_flight=True)
        self.assertEqual(self.team.tick('demo', 2)['stage'], 'waiting')
        self.assertEqual(self.store.get(run['id'])['stage'], 'blocked')

    def test_target_does_not_reconcile_or_notify_other_ready_runs(self):
        other = self.store.create(self.store.project('demo'), self.github.items[1])
        self.store.save(other, stage='ready', notification_pending=True)
        with patch.object(self.team, 'reconcile') as reconcile, patch.object(self.team, 'notify') as notify:
            target = self.team.tick('demo', 2)
            reconcile.assert_not_called()
            self.assertTrue(all(call.args[1]['issue'] == 2 for call in notify.call_args_list))
        self.assertEqual(target['issue'], 2)
        self.assertTrue(self.store.get(other['id'])['notification_pending'])

    def test_target_quota_wait_retains_automatic_cooldown(self):
        run = self.team.tick('demo', 2)
        self.store.save(run, stage='quota_wait', retry_at=10**20, resume_stage='prepare')
        self.assertEqual(self.team.tick('demo', 2)['stage'], 'quota_wait')
        self.store.save(run, retry_at=0)
        self.assertEqual(self.team.tick('demo', 2)['stage'], 'implement')
        self.assertEqual(len(self.store.runs()), 1)

    def test_queue_cli_set_reorder_clear(self):
        def command(*args):
            with self.store.lock(), patch('agent_team.cli.emit'):
                return dispatch(parser().parse_args(args), self.store)
        command('queue', 'set', 'demo', '3, 1, 2')
        command('queue', 'reorder', 'demo', '2,3,1')
        self.assertEqual(self.store.project('demo')['queue_order'], [2, 3, 1])
        with self.assertRaises(TeamError):
            command('queue', 'reorder', 'demo', '1,2')
        command('queue', 'clear', 'demo')
        self.assertEqual(self.store.project('demo')['queue_order'], [])

    def test_queue_mutations_respect_worker_lock(self):
        other = Store(self.temp.name)
        try:
            with self.store.lock(), self.assertRaises(TeamError):
                with other.lock():
                    other.set_queue('demo', [2])
        finally:
            other.db.close()

    def test_targeted_watch_stops_at_boundaries(self):
        for stage in ('stopped', 'ready', 'blocked', 'handoff', 'repair', 'stale', 'waiting', 'paused', 'closed', 'merged'):
            with self.subTest(stage=stage), patch('agent_team.cli.Coordinator') as team, \
                    patch('agent_team.cli.emit'), patch('agent_team.cli.time.sleep') as sleep:
                team.return_value.tick.return_value = {'stage': stage}
                dispatch(parser().parse_args(['run', 'demo', '--issue', '2', '--watch']), self.store)
                team.return_value.tick.assert_called_once_with('demo', 2)
                sleep.assert_not_called()

    def test_targeted_watch_passes_explicit_stop_boundary(self):
        with patch('agent_team.cli.Coordinator') as team, patch('agent_team.cli.emit'), \
                patch('agent_team.cli.time.sleep') as sleep:
            team.return_value.tick.return_value = {'stage': 'stopped'}
            dispatch(parser().parse_args([
                'run', 'demo', '--issue', '2', '--stop-after', 'validate', '--watch',
            ]), self.store)
            team.return_value.tick.assert_called_once_with('demo', 2, 'validate')
            sleep.assert_not_called()

    def test_untargeted_watch_continues_and_target_quota_waits(self):
        for selection in ([], ['--issue', '2']):
            with patch('agent_team.cli.Coordinator') as team, patch('agent_team.cli.emit'), \
                    patch('agent_team.cli.time.sleep', side_effect=KeyboardInterrupt) as sleep:
                team.return_value.tick.return_value = {'stage': 'quota_wait' if selection else 'ready'}
                with self.assertRaises(KeyboardInterrupt):
                    dispatch(parser().parse_args(['run', 'demo', '--watch'] + selection), self.store)
                sleep.assert_called_once()
