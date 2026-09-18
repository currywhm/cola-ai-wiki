"""Regression tests for Harness tool event normalization.

The small program receives the same ``session.event`` notifications the bridge
does in production. The tests focus on the contract the mini program consumes:
stable call ids, nested tool-result extraction, and typed result metadata.
"""
import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

from app.services import harness


class _Notification:
    def __init__(self, event):
        self.method = 'session.event'
        self.payload = {'event': event}


class _Result:
    def __init__(self, final_response=''):
        self.final_response = final_response
        self.events = []


def _notification(event_type, data):
    return _Notification({'type': event_type, 'data': data})


class HarnessEventTests(unittest.TestCase):
    def test_selected_skill_prompt_uses_official_invocation_and_hides_catalog(self):
        prompt = harness._selected_skills_prompt(
            '有哪些技能',
            '',
            'tenant-a',
            [{'id': 'builtin-write-report', 'harness': 'write-report', 'label': '撰写报告'}],
        )

        self.assertTrue(prompt.startswith('/write-report\n'))
        self.assertIn('本轮用户已明确选择技能：撰写报告', prompt)
        self.assertIn('不要向用户逐个罗列全部技能', prompt)
        self.assertIn('只说明本轮已启用的技能', prompt)

    def test_omitted_skills_can_use_saved_selection_but_explicit_empty_clears_it(self):
        saved = ['builtin-write-report']

        self.assertEqual(
            harness.select_turn_skill_ids(None, '', saved),
            ['builtin-write-report'],
        )
        self.assertEqual(harness.select_turn_skill_ids([], '', saved), [])
        self.assertEqual(
            harness.select_turn_skill_ids([], 'builtin-make-deck', saved),
            [],
        )
        self.assertEqual(
            harness.select_turn_skill_ids(None, 'builtin-make-deck', saved),
            ['builtin-make-deck'],
        )

    def test_nested_tool_result_is_unwrapped(self):
        blocks = [{
            'type': 'tool-result',
            'toolCallId': 'call-1',
            'isError': True,
            'content': [{'type': 'text', 'text': 'command failed'}],
        }]

        nested, call_id, failed = harness._tool_result_content(blocks)

        self.assertEqual(call_id, 'call-1')
        self.assertTrue(failed)
        self.assertEqual(nested, [{'type': 'text', 'text': 'command failed'}])

    def test_parallel_same_name_calls_keep_their_own_contracts(self):
        state = harness._TraceState('session-1')
        emitted = []
        emit = lambda kind, payload, pace: emitted.append((kind, payload, pace))

        harness._forward(_notification('tool/call', {
            'callId': 'read-a',
            'name': 'read',
            'arguments': json.dumps({'path': 'a.py', 'offset': 1}),
        }), emit, state)
        harness._forward(_notification('tool/call', {
            'callId': 'read-b',
            'name': 'read',
            'arguments': json.dumps({'path': 'b.py', 'offset': 3}),
        }), emit, state)

        result_b = [{
            'type': 'tool-result',
            'toolCallId': 'read-b',
            'content': [{'type': 'text', 'text': 'b content'}],
        }]
        result_a = [{
            'type': 'tool-result',
            'toolCallId': 'read-a',
            'content': [{'type': 'text', 'text': 'a content'}],
        }]
        harness._forward(_notification('tool/result', {
            'message': {'source': {'callId': 'read-b'}, 'content': result_b},
        }), emit, state)
        harness._forward(_notification('tool/result', {
            'message': {'source': {'callId': 'read-a'}, 'content': result_a},
        }), emit, state)

        tool_items = [payload['item'] for kind, payload, _ in emitted if kind == 'trace' and payload['item']['kind'] == 'tool']
        run_items = [item for item in tool_items if item['state'] == 'run']
        done_items = [item for item in tool_items if item['state'] == 'done']

        self.assertEqual([item['call_id'] for item in run_items], ['read-a', 'read-b'])
        self.assertEqual([item['tool_meta']['path'] for item in run_items], ['a.py', 'b.py'])
        self.assertEqual([item['call_id'] for item in done_items], ['read-b', 'read-a'])
        self.assertEqual([item['tool_output'] for item in done_items], ['b content', 'a content'])
        self.assertEqual(state.tools, {})

    def test_result_meta_translates_typed_tool_payloads(self):
        read_meta = harness._tool_result_meta('read', {
            'path': 'server/app.py',
            'offset': 4,
            'totalLines': 12,
            'lang': 'python',
            'lines': [{'number': 4, 'text': 'print(1)'}],
        }, [], '')
        self.assertEqual(read_meta['kind'], 'read')
        self.assertEqual(read_meta['path'], 'server/app.py')
        self.assertEqual(read_meta['offset'], 4)
        self.assertEqual(read_meta['total_lines'], 12)
        self.assertEqual(read_meta['lines'][0]['number'], 4)

        search_meta = harness._tool_result_meta('web_search', {
            'sources': [{
                'url': 'https://example.com',
                'title': 'Example',
                'snippet': 'A result',
            }],
            'truncated': False,
        }, [], '')
        self.assertEqual(search_meta['kind'], 'search')
        self.assertEqual(search_meta['sources'][0]['title'], 'Example')

    def test_presented_files_become_artifact_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / 'workspace'
            workspace.mkdir()
            report = workspace / 'report.md'
            report.write_text('# report', encoding='utf-8')
            outside = Path(tmp) / 'outside.md'
            outside.write_text('secret', encoding='utf-8')

            state = harness._TraceState('session-1', workspace)
            emitted = []
            emit = lambda kind, payload, pace: emitted.append((kind, payload, pace))

            harness._forward(_notification('deliverables/presented', {
                'turn': 1,
                'callId': 'present-1',
                'files': [
                    {'path': 'report.md', 'description': '最终报告'},
                    {'path': '../outside.md', 'description': '不得越过租户工作区'},
                ],
            }), emit, state)

            artifacts = [payload['artifact'] for kind, payload, _ in emitted if kind == 'artifact']
            self.assertEqual(len(artifacts), 1)
            self.assertEqual(artifacts[0]['path'], str(report.resolve()))
            self.assertEqual(artifacts[0]['name'], 'report.md')
            self.assertEqual(artifacts[0]['description'], '最终报告')

    def test_terminal_exit_code_is_exposed_to_the_card(self):
        result = harness._tool_result_contract(
            'bash',
            [{'type': 'text', 'text': 'failed\n[exit code: 7]'}],
            None,
            {},
        )

        self.assertEqual(result['tool_result_meta']['exit_code'], 7)
        self.assertIn('failed', result['tool_output'])

    def test_nested_failure_uses_output_as_error_copy(self):
        result = harness._tool_result_contract(
            'read',
            [{'type': 'text', 'text': 'permission denied'}],
            None,
            {},
            True,
        )

        self.assertEqual(result['tool_error'], 'permission denied')
        self.assertEqual(result['tool_result_summary'], 'permission denied')


class HarnessContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_collision_retries_on_new_official_session(self):
        calls = []

        def fake_run(prompt, session_id, *args):
            calls.append((prompt, session_id))
            if len(calls) == 1:
                raise RuntimeError('session "persisted-id" already exists')
            return _Result('继续回答成功'), None

        with (
            mock.patch.object(harness, 'configured', return_value=True),
            mock.patch.object(harness, 'thinking_config', return_value=('sdk', 'deepseek-v4-flash', 'low')),
            mock.patch.object(harness, 'sync_skills'),
            mock.patch.object(harness, '_run_turn', side_effect=fake_run),
        ):
            events = [
                event async for event in harness._stream_turn(
                    '继续刚才的任务',
                    '',
                    'persisted-id',
                    'deepseek-v4-flash',
                    'quick',
                    None,
                    'tenant-a',
                    with_usage=True,
                    history=[
                        {'role': 'user', 'content': '项目代号是什么'},
                        {'role': 'assistant', 'content': '蓝色海豚'},
                    ],
                )
            ]

        session_ids = [event['session_id'] for event in events if event['kind'] == 'session']
        self.assertEqual(session_ids[0], 'persisted-id')
        self.assertNotEqual(session_ids[1], 'persisted-id')
        self.assertEqual(len(calls), 2)
        self.assertIn('蓝色海豚', calls[1][0])
        self.assertIn('当前用户消息', calls[1][0])
        self.assertEqual(''.join(event['text'] for event in events if event['kind'] == 'text'), '继续回答成功')

    def test_session_collision_recognition_is_narrow(self):
        self.assertTrue(harness._is_session_exists_error(RuntimeError('session "x" already exists')))
        self.assertFalse(harness._is_session_exists_error(RuntimeError('network unavailable')))


if __name__ == '__main__':
    unittest.main()
