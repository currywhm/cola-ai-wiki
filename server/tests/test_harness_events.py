"""Regression tests for Harness tool event normalization.

The small program receives the same ``session.event`` notifications the bridge
does in production. The tests focus on the contract the mini program consumes:
stable call ids, nested tool-result extraction, and typed result metadata.
"""
import json
import unittest

from app.services import harness


class _Notification:
    def __init__(self, event):
        self.method = 'session.event'
        self.payload = {'event': event}


def _notification(event_type, data):
    return _Notification({'type': event_type, 'data': data})


class HarnessEventTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
