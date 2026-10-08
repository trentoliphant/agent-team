"""Provider-free acceptance coverage for the human-first preview."""
import json
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_team.agents import Agents
from agent_team.cli import dispatch, parser, main
from agent_team.coordinator import Coordinator, outcome_comment, feedback_findings, response_comment
from agent_team.github import GitHub
from agent_team.process import execute, metadata, TeamError, ModelCapacityError, QuotaError
from agent_team.state import CapacityWait, Store
from agent_team.trace import trace_report, format_trace
from scripts.install_preview import main as install_preview
import test_coordinator as workflow


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.github = GitHub()
        self.github._login = 'worker[bot]'
        self.project = {'repo': 'owner/repo', 'ready_label': 'agent:ready',
                        'approvers': [{'id': 7, 'login': 'maintainer'}]}
        self.issue = {'number': 1, 'title': 'Task', 'body': 'Acceptance', 'state': 'open'}
        self.comment = {'id': 9, 'node_id': 'comment9', 'body': self.github.approval_text(self.issue),
                        'created_at': '2026-10-07T00:00:00Z', 'updated_at': '2026-10-07T00:00:00Z',
                        'user': {'id': 7, 'login': 'maintainer', 'type': 'User'}}

    def authorized(self):
        with patch.object(self.github, 'pages', return_value=[self.comment]), \
                patch.object(self.github, 'api', return_value={'data': {'node': {
                    'body': self.comment['body'], 'lastEditedAt': None,
                    'author': {'__typename': 'User', 'databaseId': 7}}}}) as api:
            result = self.github.authorized(self.project, self.issue)
            self.assertTrue(all(c.args[0] == 'graphql' for c in api.call_args_list))
            return result

    def test_human_id_survives_rename_but_not_login_reuse(self):
        self.assertTrue(self.authorized())
        self.comment['user']['login'] = 'renamed'
        self.assertTrue(self.authorized())
        self.comment['user'].update(id=8, login='maintainer')
        self.assertFalse(self.authorized())

    def test_bot_edited_quoted_missing_timestamp_and_stale_approval_fail_closed(self):
        original = json.loads(json.dumps(self.comment))
        mutations = [('user', {'id': 7, 'type': 'Bot', 'login': 'maintainer'}),
                     ('updated_at', 'later'), ('body', '> ' + original['body']),
                     ('body', original['body'] + '\nI am quoting this template.'),
                     ('created_at', None)]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.comment = {**original, key: value}
                self.assertFalse(self.authorized())
        self.comment = original
        self.issue['body'] = 'Edited scope'
        self.assertFalse(self.authorized())

    def test_edit_within_creation_second_missing_node_and_changed_graphql_body_fail_closed(self):
        node = {'body': self.comment['body'], 'lastEditedAt': None,
                'author': {'__typename': 'User', 'databaseId': 7}}
        for value in ({**node, 'lastEditedAt': self.comment['created_at']},
                      {**node, 'body': 'changed after REST read'},
                      {**node, 'author': {'__typename': 'Bot', 'databaseId': 7}}, {}, None):
            with self.subTest(value=value), patch.object(self.github,'pages',return_value=[self.comment]), \
                    patch.object(self.github,'api',return_value={'data':{'node':value}}):
                self.assertFalse(self.github.authorized(self.project,self.issue))

    def test_browser_line_endings_and_trailing_whitespace_preserve_exact_template(self):
        self.comment['body'] = self.comment['body'].replace('\n','\r\n') + '\r\n  \t'
        self.assertTrue(self.authorized())
        self.comment['body'] = ' ' + self.comment['body']
        self.assertFalse(self.authorized())

    def test_bot_or_untrusted_user_cannot_approve_before_any_write(self):
        for actor in ({'id': 7, 'type': 'Bot', 'login': 'worker[bot]'},
                      {'id': 8, 'type': 'User', 'login': 'outsider'}):
            self.github._identity = actor
            with patch.object(self.github, 'api') as api, patch.object(self.github, 'setup') as setup:
                with self.assertRaisesRegex(TeamError, 'human approver'):
                    self.github.approve(self.project, 1)
                api.assert_not_called()
                setup.assert_not_called()

    def test_approval_creates_new_exact_comment_and_label_without_editing(self):
        self.github._identity = self.comment['user']
        with patch.object(self.github, 'issue', return_value=self.issue), \
                patch.object(self.github, 'setup'), patch.object(self.github, 'api') as api:
            self.github.approve(self.project, 1)
            self.assertEqual(api.call_args_list[0].args,
                             ('repos/owner/repo/issues/1/comments', 'POST', {'body': self.comment['body']}))
            self.assertEqual(api.call_args_list[1].args[1], 'POST')

    def test_approver_resolution_rejects_bot_and_saves_numeric_id(self):
        with patch.object(self.github, 'api', return_value={'id': 7, 'type': 'User', 'login': 'Canonical'}):
            self.assertEqual(self.github.approvers(['canonical']), [{'id': 7, 'login': 'Canonical'}])
        with patch.object(self.github, 'api', return_value={'id': 7, 'type': 'Bot', 'login': 'worker'}):
            with self.assertRaises(TeamError):
                self.github.approvers(['worker'])

    def test_unchanged_comment_and_retired_parts_do_not_patch(self):
        self.github._login = 'worker'
        comments = [{'id': 1, 'body': '<!-- agent-team:x -->\nStatus', 'user': {'login': 'worker'}},
                    {'id': 2, 'body': '<!-- agent-team:x-part-2 -->\n*No longer used; the updated text is in the comments above.*',
                     'user': {'login': 'worker'}}]
        with patch.object(self.github, 'pages', return_value=comments), patch.object(self.github, 'api') as api:
            self.github.comment('owner/repo', 1, 'x', 'Status')
            api.assert_not_called()
            self.github.comment('owner/repo', 1, 'x', 'Changed')
            api.assert_called_once()


class InstallerTests(unittest.TestCase):
    def test_same_version_preview_updates_reinstall_the_requested_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            prefix, bin_dir = Path(temp).resolve()/'preview', Path(temp).resolve()/'bin'
            for sha in ('a'*40,'b'*40):
                with patch('sys.argv',['install_preview.py','--ref',sha,'--prefix',str(prefix),
                                       '--bin-dir',str(bin_dir)]), patch('sys.stdout',io.StringIO()), \
                        patch('scripts.install_preview.venv.EnvBuilder') as builder, \
                        patch('scripts.install_preview.subprocess.run') as command:
                    install_preview()
                builder.return_value.create.assert_called_once_with(prefix/'venv')
                args = command.call_args.args[0]
                self.assertIn('--force-reinstall',args)
                self.assertEqual(args[-1],f'git+https://github.com/trentoliphant/agent-team.git@{sha}')
                wrapper = (bin_dir/'agent-team-preview').read_text()
                self.assertIn(str(prefix/'state'),wrapper)
                self.assertIn(str(prefix/'venv/bin/agent-team'),wrapper)


class ProcessTests(unittest.TestCase):
    def test_partial_output_survives_timeout_and_decodes_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            out, err = Path(temp) / 'out', Path(temp) / 'err'
            command = [sys.executable, '-u', '-c',
                       "import os,time; os.write(1,b'partial\\xff'); os.write(2,b'diagnostic'); time.sleep(10)"]
            with self.assertRaisesRegex(TeamError, 'Timed out'):
                execute(command, timeout=.2, stdout_path=out, stderr_path=err)
            self.assertEqual(out.read_text(errors='replace'), 'partial\ufffd')
            self.assertEqual(err.read_text(), 'diagnostic')

    def test_live_log_visible_before_communicate_finishes_and_interrupt_retains_it(self):
        with tempfile.TemporaryDirectory() as temp:
            out, err = Path(temp) / 'out', Path(temp) / 'err'
            process = unittest.mock.MagicMock(pid=12345)
            def communicate(*args, **kwargs):
                if process.communicate.call_count == 1:
                    out.write_text('live')
                    self.assertEqual(out.read_text(), 'live')
                    raise KeyboardInterrupt()
                return None, None
            process.communicate.side_effect = communicate
            with patch('agent_team.process.subprocess.Popen', return_value=process), \
                    patch('agent_team.process.os.killpg') as kill:
                with self.assertRaises(KeyboardInterrupt):
                    execute(['fake'], stdout_path=out, stderr_path=err)
                kill.assert_called_once()
            self.assertEqual(out.read_text(), 'live')

    def test_substitution_mechanisms_are_rejected_before_git(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / '.git').mkdir()
            for rel in ('refs/replace/a', 'info/grafts', 'shallow', 'objects/info/alternates',
                        'objects/info/http-alternates', 'packed-refs'):
                path = root / '.git' / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('a refs/replace/b\n' if rel == 'packed-refs' else 'substitute')
                with self.subTest(rel=rel), self.assertRaisesRegex(TeamError, 'substitutions'):
                    metadata(root)
                path.unlink()

    def test_streamed_claude_result_preserves_usage_and_rejects_missing_or_multiple_results(self):
        report = {'verdict': 'pass', 'summary': 'ok', 'findings': []}
        final = {'type': 'result', 'is_error': False, 'structured_output': report,
                 'modelUsage': {'claude-opus-5-5': {}}, 'usage': {'input_tokens': 12}}
        with tempfile.TemporaryDirectory() as temp:
            def call(messages):
                with patch('agent_team.agents.subscription_status', return_value='fixture'), \
                        patch('agent_team.agents.execute', return_value=subprocess.CompletedProcess(
                            [], 0, '\n'.join(json.dumps(m) for m in messages), '')) as command:
                    result = Agents().run('claude', 'review', 'Review', Path(temp), Path(temp)/'artifacts', {'timeout': 30})
                    self.assertIn('stream-json', command.call_args.args[0])
                    self.assertIn('stdout_path', command.call_args.kwargs)
                    return result
            self.assertEqual(call([{'type': 'assistant'}, final])['usage'], {'input_tokens': 12})
            for messages in ([{'type': 'assistant'}], [final, final], [final, {'type': 'assistant'}],
                             [{'type': 'result', 'is_error': True, 'result': 'bad'}]):
                with self.subTest(messages=messages), self.assertRaises(TeamError):
                    call(messages)
            for text, error in (('Selected model is at capacity', ModelCapacityError), ('Rate limit exceeded', QuotaError)):
                with self.assertRaises(error):
                    call([{**final, 'is_error': True, 'result': text}])
    def test_mixed_capacity_and_subscription_messages_keep_long_quota_classification(self):
        for text in ('Usage limit reached; model is at capacity', 'Model is at capacity; rate limit exceeded'):
            with self.subTest(text=text), self.assertRaises(QuotaError) as error:
                Agents.raise_failure(text)
            self.assertNotIsInstance(error.exception,ModelCapacityError)



class RegistryTests(unittest.TestCase):
    def test_new_defaults_and_legacy_project_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            self.addCleanup(store.db.close)
            project = store.register('new', 'owner/new', 'main', ['true'])
            self.assertEqual((project['max_revisions'], project['minor_cleanup'], project['status_mode']), (4, False, 'template'))
            legacy = {k: v for k, v in project.items() if k not in (
                'minor_cleanup','status_mode','status_timeout','capacity_cooldown','max_capacity_retries')}
            legacy.update(name='legacy', repo='owner/legacy', max_revisions=2)
            store.save_project(legacy)
            self.assertEqual(store.project('legacy'), legacy)
            self.assertTrue(Coordinator.cleanup_allowed(legacy, {'round': 0}))
            self.assertFalse(Coordinator.cleanup_allowed(project, {'round': 0}))

    def test_newer_registry_rejected_without_changing_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            store.db.execute("UPDATE meta SET value='9.0.0' WHERE key='registry_version'")
            store.db.commit()
            store.db.close()
            with self.assertRaisesRegex(TeamError, 'newer'):
                Store(temp)
            db = sqlite3.connect(Path(temp)/'state.sqlite3')
            self.addCleanup(db.close)
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='registry_version'").fetchone()[0], '9.0.0')

    def test_registry_suffix_and_unrecognized_stamps_fail_cleanly_in_cli(self):
        for stamp in ('1.0rc1','future'):
            with self.subTest(stamp=stamp), tempfile.TemporaryDirectory() as temp:
                store=Store(temp)
                store.db.execute("UPDATE meta SET value=? WHERE key='registry_version'",(stamp,))
                store.db.commit();store.db.close()
                error=io.StringIO()
                with patch('sys.stderr',error), self.assertRaises(SystemExit) as exit_code:
                    main(['--home',temp,'status'])
                self.assertEqual(exit_code.exception.code,1)
                self.assertIn('newer',error.getvalue())
                self.assertNotIn('Traceback',error.getvalue())

    def test_read_only_reopen_does_not_rewrite_unchanged_version_stamp(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(temp)
            store.db.executescript("CREATE TABLE stamp_writes(value TEXT); CREATE TRIGGER record_stamp AFTER INSERT ON meta "
                                   "WHEN NEW.key='registry_version' BEGIN INSERT INTO stamp_writes VALUES(NEW.value); END;")
            store.db.close()
            store=Store(temp)
            self.addCleanup(store.db.close)
            self.assertEqual(store.db.execute('SELECT count(*) FROM stamp_writes').fetchone()[0],0)

    def test_mixed_legacy_and_new_review_history_counts_every_review_once(self):
        blocking={'severity':'blocking','location':'a:1','evidence':'bad','request':'fix'}
        minor={'severity':'minor','location':'b:1','evidence':'style','request':'rename'}
        first={'verdict':'changes_requested','summary':'bad','findings':[blocking]}
        final={'verdict':'pass','summary':'ok','findings':[minor]}
        run={'id':'r','round':1,'sha':'B','published_sha':'B','reviewed_sha':'B','review_sha':'B',
             'revision_history':[{'kind':'review','round':0,'sha':'A','review_report':first,'findings':[blocking]}],
             'review_history':[{'round':0,'sha':'A','record':{'report':first}},
                               {'round':1,'sha':'B','record':{'report':final}}],
             'review_record':{'report':final}}
        self.assertIn('2 (1 blocking, 1 minor)',outcome_comment(run,'merged'))
        run['review_history']=run['review_history'][1:]  # first review predates 0.2
        self.assertIn('2 (1 blocking, 1 minor)',outcome_comment(run,'merged'))

    def test_validation_feedback_is_not_a_missing_review_response(self):
        run={'round':1,'author':'codex','author_record':{'report':{'responses':[]}},
             'revision_history':[{'round':0,'kind':'validation','findings':[{'severity':'validation',
                 'location':'false','request':'Make command pass'}]}]}
        self.assertEqual(feedback_findings(run),[])
        self.assertNotIn('No response matched',response_comment(run,'B'))

    def test_watch_advances_progress_and_polls_pending_capacity_and_flapping(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            self.addCleanup(store.db.close)
            for waiting in ('ci', 'busy', 'quota_wait', 'ready'):
                with patch('agent_team.cli.Coordinator') as team, patch('agent_team.cli.emit'), \
                        patch('agent_team.cli.time.sleep') as sleep:
                    team.return_value.tick.side_effect = [
                        {'stage': 'implement', 'progressed': True}, {'stage': 'validate', 'progressed': True},
                        {'stage': waiting, 'progressed': False}, {'stage': 'paused'}]
                    dispatch(parser().parse_args(['run','new','--run','r','--watch']),store)
                    self.assertEqual(sleep.call_count, 1 if waiting != 'ready' else 0)
            with patch('agent_team.cli.Coordinator') as team, patch('agent_team.cli.emit'), \
                    patch('agent_team.cli.time.sleep', side_effect=KeyboardInterrupt) as sleep:
                team.return_value.tick.return_value = {'stage': 'validate', 'progressed': True}
                with self.assertRaises(KeyboardInterrupt):
                    dispatch(parser().parse_args(['run','new','--watch']),store)
                self.assertEqual(team.return_value.tick.call_count,13)
                sleep.assert_called_once()

    def test_capacity_has_shared_short_cooldown_and_smoke_leaves_no_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            self.addCleanup(store.db.close)
            with patch('agent_team.state.time.time', return_value=100):
                with self.assertRaises(ModelCapacityError), store.subscription('claude',3600,60):
                    raise ModelCapacityError('busy')
                self.assertEqual(float(store.db.execute("SELECT value FROM meta WHERE key='quota-claude'").fetchone()[0]),160)
                with self.assertRaises(CapacityWait), store.subscription('claude',3600,60):
                    self.fail('capacity reused')
            with self.assertRaises(ModelCapacityError), store.subscription('codex'):
                raise ModelCapacityError('smoke')
            self.assertIsNone(store.db.execute("SELECT value FROM meta WHERE key='quota-codex'").fetchone())
    def test_short_capacity_failure_does_not_shorten_a_longer_recorded_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(temp)
            self.addCleanup(store.db.close)
            with patch('agent_team.state.time.time',return_value=100):
                with self.assertRaises(ModelCapacityError), store.subscription('claude',3600,60):
                    store.db.execute("INSERT OR REPLACE INTO meta VALUES ('quota-claude','500')")
                    store.db.commit()
                    raise ModelCapacityError('busy')
                self.assertEqual(float(store.db.execute("SELECT value FROM meta WHERE key='quota-claude'").fetchone()[0]),500)



class PreviewWorkflowTests(unittest.TestCase):
    # Reuse the existing local Git/GitHub fixture without inheriting its entire test suite.
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    tick = workflow.WorkflowTests.tick

    def preview(self):
        self.project = self.store.update_project('demo', max_revisions=4, minor_cleanup=False,
                                                status_mode='template', capacity_cooldown=60)

    def test_minor_pass_finishes_in_six_ticks_two_calls_and_retains_human_trace(self):
        self.preview()
        self.agents.minor = [[{'severity':'minor','location':'feature.txt:1','evidence':'naming','request':'rename'}]]
        self.store.save_writing({'shared': 'Short human prose'})
        stages = [self.tick() for _ in range(6)]
        run = stages[-1]
        self.assertEqual(run['stage'],'ready')
        self.assertEqual(len(self.agents.calls),2)
        self.assertTrue(all(r.progressed for r in stages[:-1]))
        self.assertFalse(run.progressed)
        self.assertEqual(self.store.get(run['id']),run)
        self.assertNotIn('cleanup',run)
        trace = trace_report(self.store,run)
        self.assertEqual(len(trace['calls']),2)
        self.assertEqual(len(trace['reviews']),1)
        self.assertEqual(len(trace['test_attempts']),1)
        self.assertEqual(trace['approval']['user_id'],1)
        self.assertIn('minor',format_trace(trace))
        self.assertIn('1 minor',outcome_comment(run,'merged'))
        self.assertTrue(any('rename' in body for body in self.github.comments.values()))

    def test_revoked_active_approval_blocks_before_call(self):
        self.tick()
        self.github.items[0]['approved'] = False
        run = self.tick()
        self.assertEqual(run['stage'],'blocked')
        self.assertIn('revoked',run['error'])
        self.assertFalse(self.agents.calls)

    def test_busy_review_does_not_clone_or_spend_a_retry(self):
        run = self.tick(4)
        with self.store.subscription(run['reviewer']):
            with patch('agent_team.coordinator.execute') as execute_call:
                result = self.tick()
                execute_call.assert_not_called()
        self.assertEqual(result['stage'],'quota_wait')
        self.assertEqual(result['quota_attempts'],0)
        self.assertFalse(list(self.store.run_root(run).glob('review-*')))

    def test_review_git_object_mutation_rejected_but_report_and_logs_remain(self):
        run = self.tick(4)
        original = self.agents.run
        def mutating(agent,role,prompt,cwd,artifacts,project,readable=(),**options):
            record = original(agent,role,prompt,cwd,artifacts,project,readable,**options)
            if role == 'review':
                execute(['git','-C',str(cwd),'hash-object','-w','--stdin'],input='fake object')
            return record
        with patch.object(self.agents,'run',side_effect=mutating):
            result=self.tick()
        self.assertEqual(result['stage'],'blocked')
        self.assertIn('Git metadata or objects',result['error'])
        trace=trace_report(self.store,result)
        self.assertEqual(trace['calls'][-1]['record']['report']['verdict'],'pass')
        self.assertIsNone(result.get('reviewed_sha'))

    def test_review_substitutions_are_rejected_for_loose_packed_and_graft_forms(self):
        for rel in ('refs/replace/fake', 'packed-refs', 'info/grafts', 'shallow', 'objects/info/alternates'):
            with self.subTest(rel=rel):
                self.tearDown()
                self.setUp()
                self.tick(4)
                original = self.agents.run
                def mutating(agent,role,prompt,cwd,artifacts,project,readable=(),**options):
                    record = original(agent,role,prompt,cwd,artifacts,project,readable,**options)
                    path = Path(cwd)/'.git'/rel
                    path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text('a refs/replace/fake\n' if rel == 'packed-refs' else 'fake')
                    return record
                with patch.object(self.agents,'run',side_effect=mutating):
                    run = self.tick()
                self.assertEqual(run['stage'],'blocked')
                self.assertIn('substitutions',run['error'])
                self.assertIsNone(run.get('reviewed_sha'))

    def test_review_clone_failure_creates_no_subscription_cooldown_or_retry(self):
        self.tick(4)
        with patch('agent_team.coordinator.execute',side_effect=TeamError('clone failed')):
            run = self.tick()
        self.assertEqual(run['stage'],'blocked')
        self.assertEqual(run['quota_attempts'],0)
        self.assertIsNone(self.store.db.execute("SELECT value FROM meta WHERE key='quota-claude'").fetchone())
        self.assertEqual(len(self.agents.calls),1)

    def test_retry_attempts_have_distinct_paths_and_trace_is_read_only(self):
        run = self.tick()
        project=self.store.project('demo')
        artifacts=self.store.artifacts(run)/'author-0'
        with patch.object(self.agents,'run',side_effect=ModelCapacityError('busy')):
            for _ in range(2):
                with self.assertRaises(ModelCapacityError):
                    self.team.call_agent('codex','implement','',self.store.workspace(run),artifacts,project,reserved=True)
        calls=trace_report(self.store,self.store.get(run['id']))['calls']
        self.assertEqual(len(calls),2)
        self.assertNotEqual(calls[0]['artifacts'],calls[1]['artifacts'])
        with patch('agent_team.cli.GitHub') as github, patch('agent_team.cli.Coordinator') as coordinator, \
                patch.object(self.store,'save') as save, patch('agent_team.cli.emit'):
            dispatch(parser().parse_args(['trace',run['id'],'--json']),self.store)
            github.return_value.api.assert_not_called()
            coordinator.return_value.tick.assert_not_called()
            save.assert_not_called()

    def test_discovery_artifacts_do_not_create_unreachable_run_events(self):
        artifacts = self.store.home/'discovery'/'demo'/'stamp'/'artifacts'
        with patch.object(self.agents,'run',return_value={'report':{'issues':[]}}):
            result = self.team.call_agent('codex','discover','',self.root,artifacts,self.project)
        self.assertEqual(result,{'report':{'issues':[]}})
        self.assertEqual(len(list(artifacts.glob('attempt-*'))),1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM events WHERE run IS NULL').fetchone()[0],0)

    def test_transient_retries_are_bounded_separately_and_resume_resets_them(self):
        self.preview()
        self.tick()
        for attempt in range(1,4):
            with patch.object(self.agents,'run',side_effect=ModelCapacityError('busy')):
                run=self.tick()
            self.assertEqual(run['capacity_attempts'],attempt)
            self.assertEqual(run['quota_attempts'],0)
            self.assertEqual(run['stage'],'quota_wait' if attempt<3 else 'blocked')
            self.store.db.execute("DELETE FROM meta WHERE key='quota-codex'")
            self.store.db.commit()
            if attempt<3:
                self.store.save(run,retry_at=0)
        run=self.team.resume(run['id'])
        self.assertEqual(run['capacity_attempts'],0)

    def test_multi_round_trace_binds_candidates_and_preserves_unanswered_after_readiness(self):
        self.preview()
        self.agents.reject=1
        run=self.tick(10)
        self.assertEqual(run['stage'],'ready')
        trace=trace_report(self.store,run)
        authors={a['round']:a for a in trace['authors']}
        first=run['revision_history'][0]['sha']
        self.assertEqual(authors[0]['sha'],first)
        self.assertEqual(authors[1]['input_sha'],first)
        self.assertEqual(authors[1]['sha'],run['sha'])
        self.assertFalse(run['needs_revision'])
        self.assertEqual(trace['unanswered_findings'][0]['location'],'feature.txt:1')
        self.assertIn('Author round 1: No response matched',format_trace(trace))
        self.assertEqual(sum('approval' in e['changes'] for e in self.store.events(run['id'])),1)

    def test_validation_only_revision_does_not_publish_unmatched_response_comment(self):
        self.project['tests']=['false'];self.store.save_project(self.project)
        self.assertEqual(self.tick(3)['stage'],'implement')
        self.project['tests']=['test -f feature.txt'];self.store.save_project(self.project)
        run=self.tick(5)
        self.assertEqual(run['stage'],'ready')
        self.assertFalse(any('-response-' in marker for _,marker in self.github.comments))
        self.assertEqual(trace_report(self.store,run)['unanswered_findings'],[])

    def test_validation_timeout_trace_retains_reason_and_partial_output(self):
        run=self.tick()
        with self.assertRaisesRegex(TeamError,'Timed out'):
            self.team.validation_command({'timeout':.2},run,self.store.workspace(run),0,'printf partial; sleep 10')
        trace=trace_report(self.store,run)
        self.assertIsNone(trace['test_attempts'][0]['exit_code'])
        self.assertIn('Timed out',trace['test_attempts'][0]['outcome'])
        text=format_trace(trace)
        self.assertIn('no exit code',text)
        self.assertNotIn('exit None',text)
        self.assertEqual((Path(trace['test_attempts'][0]['artifacts'])/'stdout.log').read_text(),'partial')

    def test_live_attempt_is_not_mislabeled_as_interrupted(self):
        run = self.tick()
        self.store.record_event(run['id'],call_started={'agent':'codex','role':'implement','round':0,
                                                       'artifacts':'attempt','sha':None})
        self.store.save(run,in_flight=True)
        trace=trace_report(self.store,run)
        self.assertTrue(trace['in_flight'])
        self.assertIn('running or interrupted',format_trace(trace))
        self.assertNotIn('interrupted; inspect recovery state',format_trace(trace))

    def test_legacy_trace_marks_missing_information_and_unanswered_findings(self):
        run=self.tick()
        finding={'severity':'blocking','location':'file:1','evidence':'bad','request':'fix'}
        self.store.save(run,round=1,needs_revision=True,revision_history=[{'kind':'review','round':0,'findings':[finding]}],
                        author_record={'report':{'summary':'old','limitations':'','responses':[]}})
        trace=trace_report(self.store,run)
        self.assertEqual(trace['unanswered_findings'],[finding])
        self.assertIn('No response matched',format_trace(trace))
        self.assertIn('not recorded',trace['history_note'])


if __name__ == '__main__':
    unittest.main()
