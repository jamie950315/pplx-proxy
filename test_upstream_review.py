"""Regression coverage for upstream failures and owned resource lifetimes."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import server


class StreamResponse:
    status_code=200

    def __init__(self, lines):
        self.lines=lines
        self.aclose=AsyncMock()

    async def aiter_lines(self, delimiter):
        for line in self.lines:
            yield line


def answer_lines(text="answer", complete=True):
    payload={"blocks": [{"intended_usage": "ask_text", "markdown_block": {
        "progress": "DONE", "chunks": [text],
    }}]}
    lines=["event: message", "data: "+json.dumps(payload), ""]
    if complete:
        lines.extend(["event: end_of_stream", ""])
    return lines


class UpstreamReviewTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, response):
        client=server.PerplexityClient({})
        client.session=Mock(post=AsyncMock(return_value=response))
        client._initialized=True
        return client

    async def test_discovery_does_not_probe_disabled_candidates(self):
        report={"added": {}, "unavailable": [], "probed": 0}
        disabled={mid for mid, spec in server._MODEL_REGISTRY.items() if not spec.get("enabled", True)}
        with patch.object(server, "MODEL_MAP", server._default_model_map()), \
             patch.object(server, "ACCOUNT_TYPE", "max"), \
             patch.object(server, "probe_model", AsyncMock(return_value=True)) as probe:
            await server._discover_known_missing_models(object(), report, sleep_seconds=0)
        probed={call.args[1] for call in probe.call_args_list}
        for mid in disabled:
            self.assertNotIn(server._ALL_MODELS[mid][1], probed)
            self.assertNotIn(mid, report["added"])

    async def test_http_error_closes_before_terminal_error(self):
        response=StreamResponse([])
        response.status_code=503
        response.text="temporarily unavailable"
        async for chunk in self.make_client(response).search("test"):
            self.assertEqual(chunk["status_code"], 503)
            response.aclose.assert_awaited_once()

    async def test_malformed_json_is_visible_and_stream_closes(self):
        response=StreamResponse(["event: message", "data: {broken", ""])
        client=self.make_client(response)
        with self.assertRaisesRegex(RuntimeError, "malformed SSE JSON"):
            _=[chunk async for chunk in client.search("test")]
        response.aclose.assert_awaited_once()

    async def test_truncated_stream_cannot_report_success(self):
        response=StreamResponse(answer_lines(complete=False))
        client=self.make_client(response)
        with self.assertRaisesRegex(RuntimeError, "before end_of_stream"):
            _=[chunk async for chunk in client.search("test")]
        response.aclose.assert_awaited_once()

    async def test_provider_error_event_is_visible(self):
        response=StreamResponse(["event: error", "data: upstream unavailable", ""])
        with self.assertRaisesRegex(RuntimeError, "upstream unavailable"):
            _=[chunk async for chunk in self.make_client(response).search("test")]
        response.aclose.assert_awaited_once()

    async def test_empty_success_is_rejected(self):
        response=StreamResponse(answer_lines(""))
        with self.assertRaisesRegex(RuntimeError, "empty answer"):
            _=[chunk async for chunk in self.make_client(response).search("test")]

    async def test_success_closes_before_reporting_done_and_auto_is_concise(self):
        response=StreamResponse(answer_lines())
        client=self.make_client(response)
        async for chunk in client.search("test", "pro", "pplx_pro"):
            if chunk.get("done"):
                response.aclose.assert_awaited_once()
        self.assertEqual(client.session.post.call_args.kwargs["json"]["params"]["mode"], "concise")

    async def test_concurrent_initialization_uses_one_authenticated_session(self):
        response=Mock(status_code=200)
        response.json.return_value={"user": {"id": "test"}}
        session=Mock(get=AsyncMock(return_value=response), close=AsyncMock())
        client=server.PerplexityClient({})
        with patch.object(server.cffi_requests, "AsyncSession", return_value=session) as factory:
            await asyncio.gather(client.init(), client.init(), client.init())
        factory.assert_called_once()
        await client.reset({"new": "cookie"})
        session.close.assert_awaited_once()
        self.assertFalse(client._initialized)

    async def test_unauthenticated_init_closes_session_and_remains_uninitialized(self):
        response=Mock(status_code=200)
        response.json.return_value={}
        session=Mock(get=AsyncMock(return_value=response), close=AsyncMock())
        client=server.PerplexityClient({})
        with patch.object(server.cffi_requests, "AsyncSession", return_value=session):
            with self.assertRaisesRegex(RuntimeError, "not authenticated"):
                await client.init()
        session.close.assert_awaited_once()
        self.assertIsNone(client.session)
        self.assertFalse(client._initialized)

    async def test_network_failure_does_not_trigger_version_upgrades(self):
        class FailedClient:
            async def search(self, *args):
                yield {"error": "HTTP 503", "status_code": 503}
        with patch.object(server, "MODEL_MAP", {"gpt": ("pro", "gpt56_terra")}), \
             patch.object(server, "_version_upgrade_candidates") as candidates:
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                await server._try_upgrade_model(FailedClient(), "gpt", "pro", "gpt56_terra")
            candidates.assert_not_called()

    async def test_lifespan_cancels_owned_jobs_and_closes_client(self):
        started=[]
        cancelled=[]

        async def job():
            started.append(asyncio.current_task())
            try:
                await asyncio.Future()
            finally:
                cancelled.append(asyncio.current_task())

        @asynccontextmanager
        async def transport(app):
            yield

        client=Mock(close=AsyncMock())
        with patch.object(server, "_transport_lifespan", transport), \
             patch.object(server, "reconcile_configured_session", AsyncMock()), \
             patch.object(server, "_responses_load"), \
             patch.object(server, "_responses_tasks", {}), \
             patch.object(server, "session_keepalive_loop", job), \
             patch.object(server, "auto_discover_loop", job), \
             patch.object(server, "_rate_limit_poll_loop", job), \
             patch.object(server, "_client", client), \
             patch.object(server, "_rate_limit_refresh_task", None):
            async with server._service_lifespan(server.app):
                await asyncio.sleep(0)
                self.assertEqual(len(started), 3)
        self.assertEqual(len(cancelled), 3)
        client.close.assert_awaited_once()

    async def test_real_http_sse_handles_lf_and_crlf(self):
        # Feed real TCP responses through curl_cffi, not a parser-shaped fake.
        for ending in ("\n", "\r\n"):
            body=ending.join(answer_lines()+[""]).encode()

            async def handler(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             +f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()+body)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            listener=await asyncio.start_server(handler, "127.0.0.1", 0)
            port=listener.sockets[0].getsockname()[1]
            client=server.PerplexityClient({})
            client.session=server.cffi_requests.AsyncSession()
            client._initialized=True
            try:
                with patch.object(server, "PPLX_SSE_ASK", f"http://127.0.0.1:{port}/ask"):
                    chunks=[chunk async for chunk in client.search("test")]
                self.assertEqual("".join(chunk.get("delta", "") for chunk in chunks), "answer")
                self.assertEqual(chunks[-1]["answer"], "answer")
                self.assertTrue(chunks[-1]["done"])
            finally:
                await client.close()
                listener.close()
                await listener.wait_closed()


class ConfigReviewTests(unittest.TestCase):
    def test_custom_model_is_listed_for_paid_tiers_and_blocked_for_free(self):
        custom_map={"auto": ("pro", "pplx_pro"), "custom-model": ("pro", "custom_pref"),
                    "opus": ("pro", "claude48opus"), "haiku": ("pro", "claude45haiku")}
        with patch.object(server, "MODEL_MAP", custom_map):
            for tier in ("pro", "max"):
                with patch.object(server, "ACCOUNT_TYPE", tier):
                    self.assertIn("custom-model", server.get_model_map())
                    self.assertEqual(server.check_tier("custom-model"), "")
                    self.assertNotIn("haiku", server.get_model_map())
            with patch.object(server, "ACCOUNT_TYPE", "free"):
                self.assertEqual(list(server.get_model_map()), ["auto"])
                self.assertIn("requires pro", server.check_tier("custom-model"))
            with patch.object(server, "ACCOUNT_TYPE", "pro"):
                self.assertNotIn("opus", server.get_model_map())

    def test_message_delimiters_cannot_collide_in_session_keys(self):
        self.assertNotEqual(server._session_key([("user", "hello\nassistant:world")]),
                            server._session_key([("user", "hello"), ("assistant", "world")]))

    def test_stale_quota_worker_cannot_restore_previous_session_counters(self):
        response=Mock()
        payload={"status": "ok", "solution": {"status": 200, "response":
            json.dumps({"remaining_pro": 100, "remaining_research": 10})}}
        def read():
            server._reset_rate_limit()
            return json.dumps(payload).encode()
        response.read.side_effect=read
        context=Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
        with patch.object(server, "_rate_limit", dict(server._rate_limit)), \
             patch.object(server, "_rate_limit_generation", 0), \
             patch.object(server, "load_cookies", return_value={"__Secure-next-auth.session-token": "test"}), \
             patch("urllib.request.urlopen", return_value=context):
            self.assertIsNone(server._fetch_rate_limit_sync())
            self.assertIsNone(server._rate_limit["remaining_pro"])
            self.assertEqual(server._rate_limit["updated_at"], 0)

    def test_corrupt_model_map_does_not_silently_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"models.json"
            with patch.object(server, "MODELS_FILE", path):
                for content in ("{broken", '{"gpt": "pro"}', '{"gpt": ["pro", null]}'):
                    path.write_text(content)
                    with self.assertRaises(ValueError):
                        server.load_model_map()

    def test_model_map_failed_replace_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"models.json"
            path.write_text('{"gpt": ["pro", "gpt55"]}')
            with patch.object(server, "MODELS_FILE", path), \
                 patch.object(Path, "replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    server.save_model_map({"gpt": ("pro", "gpt56_terra")})
            self.assertEqual(json.loads(path.read_text())["gpt"][1], "gpt55")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_thinking_preference_follows_upgraded_base(self):
        self.assertEqual(server._thinking_model_entry("sonnet", ("pro", "claude51sonnet")),
                         ("pro", "claude51sonnetthinking"))
        self.assertEqual(server._thinking_model_entry("gpt", ("pro", "gpt57_terra")),
                         ("pro", "gpt57_terra_thinking"))
