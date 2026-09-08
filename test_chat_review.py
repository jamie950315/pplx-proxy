import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import server


class Client:
    def __init__(self, chunks):
        self.chunks=chunks
        self.calls=[]

    async def search(self, *args):
        self.calls.append(args)
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class ChatReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patches=[patch.object(server, 'API_KEY', ''), patch.object(server, '_session_cache', {}),
                      patch.object(server, '_rate_limit', {'remaining_pro': None, 'updated_at': 0, 'last_error': None}),
                      patch.object(server, '_response_suffix', return_value='')]
        for p in self.patches:
            p.start()
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.http.aclose()
        for p in reversed(self.patches):
            p.stop()

    async def test_malformed_shapes_fail_before_upstream(self):
        base={'model': 'auto', 'messages': [{'role': 'user', 'content': 'hello'}]}
        bodies=[[], None, {**base, 'model': []}, {**base, 'stream': 'false'},
                {**base, 'messages': [{'role': [], 'content': 'hi'}]},
                {**base, 'messages': [{'role': 'user', 'content': ''}]},
                {**base, 'messages': [{'role': 'assistant', 'content': 'hi'}]},
                {**base, 'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': 'https://example.com'}]}]},
                {**base, 'tools': [{'type': 'function'}]}]
        with patch.object(server, 'get_client') as client:
            for body in bodies:
                response=await self.http.post('/v1/chat/completions', json=body)
                self.assertEqual(response.status_code, 400, (body, response.text))
            client.assert_not_called()

    async def test_unsupported_generation_controls_are_rejected(self):
        base={'model': 'auto', 'messages': [{'role': 'user', 'content': 'hello'}]}
        for control in ({'temperature': 0.2}, {'max_tokens': 10}, {'n': 2}, {'stop': ['end']}, {'response_format': {'type': 'json_object'}}, {'stream_options': {'include_usage': True}}):
            with patch.object(server, 'get_client') as client:
                response=await self.http.post('/v1/chat/completions', json={**base, **control})
                self.assertEqual(response.status_code, 400, response.text)
                client.assert_not_called()

    async def test_real_user_prompt_keywords_are_preserved(self):
        result=server._prepare_pplx_from_messages([{'role': 'user', 'content': 'Explain ccsearch 技能'}], 'test')
        self.assertEqual(json.loads(result['query'])['query'], 'Explain ccsearch 技能')

    async def test_long_query_rejected_not_sliced(self):
        with self.assertRaises(server.OpenAIAPIError) as error:
            server._prepare_pplx_from_messages([{'role': 'user', 'content': 'a'*96001}], 'test')
        self.assertEqual(error.exception.code, 'context_length_exceeded')

    async def test_lobehub_instructions_on_followup(self):
        messages=[{'role': 'developer', 'content': 'You are Lobe'}, {'role': 'user', 'content': 'first'},
                  {'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'second'}]
        with patch.object(server, '_session_lookup', return_value='old'), patch.object(server, '_load_custom_prompts', return_value='local'):
            result=server._prepare_pplx_from_messages(messages, 'test')
        self.assertIsNone(result['follow_up_uuid'])
        self.assertEqual(json.loads(result['query'])['instructions'], ['local'])

    async def test_upstream_failures_are_not_successful_streams(self):
        for chunks in [[{'error': 'HTTP 500'}], [{'delta': 'partial'}], [RuntimeError('connection failed')]]:
            client=Client(chunks)
            with patch.object(server, 'get_client', return_value=client), patch.object(server, '_session_store') as save:
                response=await self.http.post('/v1/chat/completions', json={'model': 'auto', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
                self.assertIn('"error"', response.text)
                self.assertNotIn('[DONE]', response.text)
                self.assertNotIn('"finish_reason": "stop"', response.text)
                save.assert_not_called()

    async def test_split_tags_and_terminal_answer_are_complete(self):
        full='  Hello[12]<script>secret</script> world  '
        for chunks in ([{'delta': char} for char in full]+[{'done': True, 'answer': full}], [{'done': True, 'answer': full}]):
            with patch.object(server, 'get_client', return_value=Client(chunks)):
                response=await self.http.post('/v1/chat/completions', json={'model': 'auto', 'stream': True, 'messages': [{'role': 'user', 'content': 'hello'}]})
            text=''
            for line in response.text.splitlines():
                if line.startswith('data: ') and line != 'data: [DONE]':
                    chunk=json.loads(line[6:])
                    self.assertNotIn('error', chunk)
                    text+=''.join(c['delta'].get('content', '') for c in chunk['choices'])
            self.assertEqual(text, '  Hello world  ')
            self.assertIn('[DONE]', response.text)

    async def test_auto_does_not_decrement_pro(self):
        for stream in (False, True):
            client=Client([{'delta': 'ok'}, {'done': True, 'answer': 'ok'}])
            with patch.object(server, 'get_client', return_value=client), patch.object(server, '_decrement_pro') as decrement:
                response=await self.http.post('/v1/chat/completions', json={'model': 'auto', 'stream': stream, 'messages': [{'role': 'user', 'content': 'hello'}]})
                self.assertEqual(response.status_code, 200)
                decrement.assert_not_called()
                self.assertEqual(client.calls[0][1], 'auto')

    async def test_quota_exhaustion_never_switches_model(self):
        server._rate_limit['remaining_pro']=0
        with patch.object(server, 'get_client') as client:
            response=await self.http.post('/v1/chat/completions', json={'model': 'gpt', 'messages': [{'role': 'user', 'content': 'hello'}]})
            self.assertEqual(response.status_code, 429)
            client.assert_not_called()

    async def test_health_does_not_wait_for_browser(self):
        with patch.object(server, '_refresh_rate_limit', new_callable=AsyncMock) as refresh:
            await server.health()
            refresh.assert_awaited_once_with(block=False)

    async def test_bad_cookie_json_never_becomes_a_token(self):
        with patch.object(server, '_validate_session_cookies', new_callable=AsyncMock) as validate:
            for body in ({}, [], {'session_token': 123}):
                response=await self.http.post('/admin/refresh-cookie', json=body)
                self.assertEqual(response.status_code, 400)
            response=await self.http.post('/admin/refresh-cookie', content='{', headers={'content-type': 'application/json'})
            self.assertEqual(response.status_code, 400)
            validate.assert_not_called()

    async def test_failed_model_write_does_not_change_active_map(self):
        original=dict(server.MODEL_MAP)
        with patch.object(server, 'save_model_map', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.http.post('/admin/update-models', json={'models': {'new': ['pro', 'new']}})
        self.assertEqual(server.MODEL_MAP, original)

    async def test_runtime_writes_are_private_and_failed_writes_preserve_data(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data.json'
            server._write_json_atomic(path, {'original': True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with patch('server.json.dump', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    server._write_json_atomic(path, {'replacement': True})
            self.assertEqual(json.loads(path.read_text()), {'original': True})
            self.assertEqual(list(Path(directory).iterdir()), [path])

    async def test_clean_preserves_code_indentation(self):
        code='```python\nif True:\n    print(1)\n```'
        self.assertEqual(server._clean_response(code), code)


if __name__ == '__main__':
    unittest.main()
