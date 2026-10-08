"""Regressions from the first live preview trial; no provider or GitHub calls."""
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_team.coordinator import pr_body, response_comment
from agent_team.evidence import response_matches
from agent_team.trace import trace_report, format_trace
import test_coordinator as workflow


# Exact public finding and response strings from cog-smith PR15's first revision.
LIVE_LOCATION = ('templates/code-cog/src/cog_core.py:603-607, 628-632, 641-642, 654-655; '
                 'templates/context-cog/src/cog_core.py:350-353, 360-362; '
                 'templates/*/tests/test_cog.py.tmpl (before `class TestInputSeverity`)')
LIVE_RESPONSE = LIVE_LOCATION.removesuffix(' (before `class TestInputSeverity`)')


def finding(location):
    return {'severity': 'minor', 'location': location, 'evidence': 'formatting', 'request': 'Align lines'}


class ResponseMatchingTests(unittest.TestCase):
    def matches(self, locations, replies):
        return response_matches([finding(x) for x in locations],
                                [{'finding': x, 'response': 'Done'} for x in replies])

    def test_exact_matching_then_unique_annotation_fallback(self):
        cases = [
            ([LIVE_LOCATION], [LIVE_RESPONSE], [(0, 0, 'annotation')]),
            (['a.py:1'], ['a.py:1 (before class)'], [(0, 0, 'annotation')]),
            ([' a.py:1  '], ['a.py:1'], [(0, 0, 'exact')]),
            (['a.py:1', 'a.py:1'], ['a.py:1'], [(0, 0, 'exact'), (1, 0, 'exact')]),
            (['a.py:1 (x)', 'a.py:1 (y)'], ['a.py:1 (x)', 'a.py:1'],
             [(0, 0, 'exact'), (1, 1, 'annotation')]),
            (['a.py:1 (x)', 'a.py:1 (y)'], ['a.py:1'], []),
            (['a.py:1 (x)'], ['a.py:1', 'a.py:1 (y)'], []),
            (['a.py:1 (x)'], ['a.py:2'], []),
            (['a.py:1-3 (x)'], ['a.py:1'], []),
            (['a.py:func()'], ['a.py:func'], []),
            (['a.py pr_body(run)'], ['a.py pr_body'], []),
            (['a.py:1()'], ['a.py:1'], []),
            (['a.py:1 ()'], ['a.py:1'], []),
            (['a.py:1 ( )'], ['a.py:1'], []),
            (['(x)'], [''], []),
        ]
        for locations, replies, expected in cases:
            with self.subTest(locations=locations, replies=replies):
                self.assertEqual([(m['finding'], m['response'], m['kind'])
                                  for m in self.matches(locations, replies)], expected)

    def test_live_mismatch_is_consistent_in_trace_and_response_comment(self):
        expected = [finding(LIVE_LOCATION), finding('unanswered.py:1')]
        replies = [{'finding': LIVE_RESPONSE, 'response': 'Aligned lines and removed blank line.'}]
        run = {'id': 'r', 'project': 'p', 'issue': 13, 'title': 'Input warnings', 'round': 1,
               'stage': 'ready', 'author': 'codex', 'author_record': {'report': {'responses': replies}},
               'revision_history': [{'kind': 'review', 'round': 0, 'findings': expected}]}
        store = unittest.mock.Mock()
        store.events.return_value = []
        store.run_root.return_value = Path('run')
        trace = trace_report(store, run)
        self.assertEqual(trace['unanswered_findings'], [expected[1]])
        self.assertEqual(trace['response_history'][0]['matches'],
                         [{'finding': 0, 'response': 0, 'kind': 'annotation'}])
        for text in (format_trace(trace), response_comment(run, 'SHA')):
            self.assertIn('ignoring a trailing location annotation', text)
            self.assertIn('No response matched by location: unanswered.py:1', text)
            self.assertNotIn('No response matched by location: ' + LIVE_LOCATION, text)
        self.assertEqual(trace['response_history'][0]['findings'], expected)
        self.assertEqual(trace['response_history'][0]['responses'], replies)

    def test_ambiguous_annotation_matches_remain_unanswered_in_both_outputs(self):
        expected = [finding('a.py:1 (x)'), finding('a.py:1 (y)')]
        run = {'id': 'r', 'project': 'p', 'issue': 1, 'title': 'Ambiguous', 'round': 1,
               'stage': 'ready', 'author': 'codex', 'author_record': {'report': {
                   'responses': [{'finding': 'a.py:1', 'response': 'Done'}]}},
               'revision_history': [{'kind': 'review', 'round': 0, 'findings': expected}]}
        store = unittest.mock.Mock()
        store.events.return_value = []
        store.run_root.return_value = Path('run')
        trace = trace_report(store, run)
        self.assertEqual(trace['unanswered_findings'], expected)
        self.assertEqual(response_comment(run, 'SHA').count('No response matched by location'), 2)


class DescriptionTests(unittest.TestCase):
    def run_record(self):
        return {'id': 'r', 'issue': 1, 'author': 'codex', 'reviewer': 'claude', 'round': 3,
                'description': {'summary': 'Initial feature scope', 'limitations': 'Does not handle X; tests pending'},
                'author_record': {'report': {'summary': 'Fixed input rejection', 'limitations': 'No live provider test'}},
                'authored_rounds': [0, 1], 'validated_sha': 'FINAL',
                'tests': [{'command': 'tests', 'exit_code': 0}], 'validation_plan': ['tests']}

    def test_legacy_description_retains_scope_and_labels_notes_by_author_round(self):
        text = pr_body(self.run_record())
        for expected in ('Initial feature scope', 'Latest implementation update: Fixed input rejection',
                         'round initial (recorded before coordinator validation): Does not handle X; tests pending',
                         'round 1 (recorded before coordinator validation): No live provider test',
                         'Coordinator validation of `FINAL`: `tests` exit 0',
                         'supersede implementation-time test-status claims'):
            self.assertIn(expected, text)
        self.assertNotIn('round 3 (recorded', text)

    def test_all_round_limitations_are_preserved_and_missing_validation_is_explicit(self):
        run = self.run_record()
        run['description_history'] = [dict(run['description'], round=0),
                                      {'round': 1, 'summary': 'Middle', 'limitations': 'Intermediate limit'},
                                      dict(run['author_record']['report'], round=2)]
        run['validated_sha'] = None
        run['validation_plan'].append('not run')
        text = pr_body(run)
        for expected in ('Intermediate limit', 'No live provider test', 'Does not handle X',
                         'Coordinator validation of `none recorded`', '`not run` omitted after an earlier failure'):
            self.assertIn(expected, text)


class PublicationTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    tick = workflow.WorkflowTests.tick

    def test_revision_publication_updates_description_without_losing_scope_limits(self):
        self.project = self.store.update_project('demo', minor_cleanup=False, status_mode='template')
        self.agents.reject = 1
        original = self.agents.run
        def reports(agent, role, *args, **kwargs):
            record = original(agent, role, *args, **kwargs)
            if role == 'implement':
                count = sum(role_ == 'implement' for _, role_ in self.agents.calls)
                record['report']['limitations'] = 'Scope limit; tests pending' if count == 1 else 'No live tests'
            return record
        with patch.object(self.agents, 'run', side_effect=reports):
            run = self.tick(5)
            initial_body = self.github.pull['body']
            self.agents.summary = 'Corrected malformed inputs'
            run = self.tick(5)
        self.assertEqual(run['stage'], 'ready')
        body = self.github.pull['body']
        self.assertNotEqual(body, initial_body)
        for expected in ('Added feature', 'Corrected malformed inputs', 'round 0', 'round 1',
                         'Scope limit; tests pending', 'No live tests', run['validated_sha']):
            self.assertIn(expected, body)
        self.assertEqual(body, pr_body(run))
        self.assertEqual(self.github.creates, 1)
        self.assertEqual([e['round'] for e in run['description_history']], [0, 1])
        self.assertIn('revision summary and limitations are appended', self.agents.prompts['implement'])
        self.github.create_pr(self.project, run, pr_body(run))
        self.assertEqual(self.github.pull['body'], body)
