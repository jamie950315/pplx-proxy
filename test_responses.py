import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class FakeRequest:
    def __init__(self, body):
        self._body=body
    async def json(self):
        return self._body


class FakeClient:
    def __init__(self, chunks=None):
        self.chunks=list(chunks or [
            {"thinking": "Searching: 2+2"},
            {"delta": "4"},
            {"done": True, "answer": "4", "backend_uuid": "backend-1", "actual_model": "pplx_pro"},
        ])
        self.calls=[]
    async def search(self, query, mode="auto", model_pref="pplx_pro", sources=None, language="en-US", follow_up_uuid=None):
        self.calls.append({"query": query, "mode": mode, "model_pref": model_pref, "follow_up_uuid": follow_up_uuid})
        for ch in self.chunks:
            yield ch


def _parse_sse(text: str):
    events=[]
    for block in text.strip().split("\n\n"):
        event=None
        data=None
        for line in block.splitlines():
            if line.startswith("event: "):
                event=line[7:]
            elif line.startswith("data: "):
                data=json.loads(line[6:])
        if event:
            events.append((event, data))
    return events


class ResponsesApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.responses_file=Path(self.temp.name) / ".responses_store.json"
        server._responses_reset_memory()
        self.client=FakeClient()
        self.patches=[
            patch.object(server, "RESPONSES_FILE", self.responses_file),
            patch.object(server, "get_client", lambda: self.client),
            patch.object(server, "_decrement_pro", lambda: None),
            patch.object(server, "_response_suffix", lambda *a, **k: ""),
            patch.object(server, "_session_cache", {}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server._responses_reset_memory()
        self.temp.cleanup()

    def test_content_parts_accept_text_and_reject_images(self):
        self.assertEqual(server._message_content_text([{"type": "output_text", "text": "hello"}]), "hello")
        with self.assertRaises(server.OpenAIAPIError):
            server._message_content_text([{"type": "input_image", "image_url": "https://example.com/a.png"}])

    def test_parse_string_and_developer_message(self):
        messages=server._responses_parse_input(
            [{"role": "developer", "content": "Be brief"}, {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]}],
            instructions="Always cite",
        )
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "Hi"})

    def test_create_retrieve_delete_and_input_items(self):
        async def run():
            created=await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "What is 2+2?",
                "metadata": {"source": "test"},
            }))
            self.assertEqual(created["object"], "response")
            self.assertEqual(created["status"], "completed")
            self.assertEqual(created["output_text"], "4")
            self.assertEqual(created["usage"]["input_tokens"] + created["usage"]["output_tokens"], created["usage"]["total_tokens"])
            self.assertIn("input_tokens", created["usage"])
            self.assertTrue(any(item.get("type") == "reasoning" for item in created["output"]))
            self.assertTrue(any(item.get("type") == "message" for item in created["output"]))
            self.assertFalse(any(k.startswith("_") for k in created))

            retrieved=await server.retrieve_response(created["id"])
            self.assertEqual(retrieved["id"], created["id"])
            self.assertEqual(retrieved["metadata"]["source"], "test")

            listed=await server.list_response_input_items(created["id"])
            self.assertEqual(listed["object"], "list")
            self.assertEqual(listed["data"][0]["role"], "user")
            self.assertEqual(listed["data"][0]["content"][0]["text"], "What is 2+2?")

            deleted=await server.delete_response(created["id"])
            self.assertEqual(deleted, {"id": created["id"], "object": "response", "deleted": True})
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                await server.retrieve_response(created["id"])
            self.assertEqual(ctx.exception.status_code, 404)
            self.assertEqual(ctx.exception.err_type, "invalid_request_error")

        asyncio.run(run())

    def test_previous_response_id_continues_conversation(self):
        async def run():
            first=await server.responses_api(FakeRequest({"model": "auto", "input": "What is 2+2?"}))
            self.client.chunks=[
                {"delta": "still 4"},
                {"done": True, "answer": "still 4", "backend_uuid": "backend-2"},
            ]
            second=await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "And again?",
                "previous_response_id": first["id"],
            }))
            self.assertEqual(second["previous_response_id"], first["id"])
            self.assertEqual(second["output_text"], "still 4")
            self.assertEqual(len(self.client.calls), 2)
            self.assertEqual(self.client.calls[1]["follow_up_uuid"], "backend-1")
            self.assertEqual(self.client.calls[1]["query"], "And again?")

        asyncio.run(run())

    def test_previous_and_conversation_conflict(self):
        async def run():
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                await server.responses_api(FakeRequest({
                    "model": "auto",
                    "input": "Hi",
                    "previous_response_id": "resp_missing",
                    "conversation": "conv_1",
                }))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.param, "previous_response_id")
        asyncio.run(run())

    def test_conversation_id_reuses_last_response(self):
        async def run():
            first=await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "What is 2+2?",
                "conversation": "conv_test",
            }))
            self.assertEqual(first["conversation"]["id"], "conv_test")
            self.client.chunks=[
                {"delta": "four"},
                {"done": True, "answer": "four", "backend_uuid": "backend-2"},
            ]
            second=await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "repeat",
                "conversation": "conv_test",
            }))
            self.assertEqual(second["previous_response_id"], first["id"])
            self.assertEqual(self.client.calls[1]["follow_up_uuid"], "backend-1")
        asyncio.run(run())

    def test_streaming_events_are_official_shaped(self):
        async def run():
            resp=await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "What is 2+2?",
                "stream": True,
            }))
            chunks=[]
            async for item in resp.body_iterator:
                chunks.append(item.decode() if isinstance(item, bytes) else item)
            events=_parse_sse("".join(chunks))
            names=[name for name, _data in events]
            self.assertEqual(names[0], "response.created")
            self.assertEqual(names[1], "response.in_progress")
            self.assertIn("response.reasoning_summary_text.delta", names)
            self.assertIn("response.output_text.delta", names)
            self.assertIn("response.content_part.added", names)
            self.assertEqual(names[-1], "response.completed")
            created=events[0][1]
            self.assertEqual(created["type"], "response.created")
            self.assertEqual(created["response"]["object"], "response")
            completed=events[-1][1]
            self.assertEqual(completed["response"]["status"], "completed")
            self.assertEqual(completed["response"]["output_text"], "4")
            self.assertIn("sequence_number", created)
            stored=await server.retrieve_response(created["response"]["id"])
            self.assertEqual(stored["output_text"], "4")
        asyncio.run(run())

    def test_cancel_in_progress_background_response(self):
        started=asyncio.Event()
        release=asyncio.Event()

        class SlowClient:
            async def search(self, *args, **kwargs):
                started.set()
                await release.wait()
                yield {"done": True, "answer": "late", "backend_uuid": "backend-slow"}

        async def run():
            with patch.object(server, "get_client", lambda: SlowClient()):
                created=await server.responses_api(FakeRequest({
                    "model": "auto",
                    "input": "slow please",
                    "background": True,
                }))
                self.assertEqual(created["status"], "in_progress")
                await asyncio.wait_for(started.wait(), timeout=2)
                cancelled=await server.cancel_response(created["id"])
                self.assertEqual(cancelled["status"], "cancelled")
                release.set()
                await asyncio.sleep(0.05)
                stored=await server.retrieve_response(created["id"])
                self.assertEqual(stored["status"], "cancelled")
        asyncio.run(run())

    def test_json_schema_appends_instruction(self):
        async def run():
            await server.responses_api(FakeRequest({
                "model": "auto",
                "input": "Give a number",
                "text": {"format": {"type": "json_object"}},
            }))
            query=self.client.calls[0]["query"]
            payload=json.loads(query)
            self.assertTrue(any("JSON object" in item for item in payload.get("instructions", [])))
        asyncio.run(run())

    def test_unknown_model_uses_openai_error(self):
        async def run():
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                await server.responses_api(FakeRequest({"model": "not-a-model", "input": "Hi"}))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.param, "model")
        asyncio.run(run())

    def test_store_false_is_not_retained(self):
        async def run():
            created=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "store": False}))
            self.assertEqual(created["status"], "completed")
            self.assertFalse(server._responses_store)
            self.assertFalse(self.responses_file.exists())
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                await server.retrieve_response(created["id"])
            self.assertEqual(ctx.exception.status_code, 404)
        asyncio.run(run())

    def test_invalid_request_shapes_are_rejected_before_upstream(self):
        async def run():
            invalid=[{"stream": "false"}, {"background": "false"}, {"model": []},
                {"previous_response_id": {}}, {"conversation": {"id": []}},
                {"text": {"format": []}}, {"reasoning": "high"}, {"tools": {}},
                {"tools": [{"type": "function", "name": "test"}]}, {"tool_choice": "required"},
                {"temperature": 0.2}, {"top_p": 0.5}, {"max_output_tokens": 100},
                {"text": {"format": {"type": "json_schema", "strict": True, "schema": {}}}},
                {"background": True, "store": False}, {"input": [{"type": "item_reference", "id": "missing"}]}]
            for fields in invalid:
                with self.subTest(fields=fields), self.assertRaises(server.OpenAIAPIError) as ctx:
                    await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", **fields}))
                self.assertEqual(ctx.exception.status_code, 400)
            self.assertFalse(self.client.calls)
        asyncio.run(run())

    def test_corrupt_store_is_not_overwritten(self):
        self.responses_file.write_text("{broken")
        with self.assertRaises(json.JSONDecodeError):
            server._responses_load()
        self.assertFalse(server._responses_loaded)
        self.assertEqual(self.responses_file.read_text(), "{broken")
        with self.assertRaises(json.JSONDecodeError):
            server._responses_put({"id": "resp_new", "created_at": 0})
        self.assertEqual(self.responses_file.read_text(), "{broken")

    def test_persistence_failure_is_reported(self):
        with patch("server.json.dump", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                server._responses_persist()

    def test_expired_records_and_conversation_indexes_are_removed_on_read(self):
        async def run():
            created=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "conversation": "conv_expired"}))
            with patch.object(server.time, "time", return_value=created["created_at"]+server._RESPONSES_MAX_AGE+1):
                with self.assertRaises(server.OpenAIAPIError):
                    await server.retrieve_response(created["id"])
                self.assertNotIn("conv_expired", server._conversations_index)
        asyncio.run(run())

    def test_eviction_does_not_discard_running_jobs(self):
        now=server.time.time()
        with patch.object(server, "_RESPONSES_MAX_ENTRIES", 1):
            server._responses_put({"id": "resp_running", "status": "in_progress", "created_at": now})
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                server._responses_put({"id": "resp_new", "status": "in_progress", "created_at": now})
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertIsNotNone(server._responses_get("resp_running"))

    def test_incomplete_upstream_never_reports_success(self):
        async def run():
            for chunks in ([], [{"delta": "partial"}], [{"done": True, "answer": ""}]):
                self.client.chunks=chunks
                with self.subTest(chunks=chunks), self.assertRaises(server.OpenAIAPIError) as ctx:
                    await server.responses_api(FakeRequest({"model": "auto", "input": "Hi"}))
                self.assertEqual(ctx.exception.status_code, 502)
            self.assertTrue(all(rec["status"] == "failed" for rec in server._responses_store.values()))
        asyncio.run(run())

    def test_stream_transport_failure_is_terminal(self):
        class BrokenClient:
            async def search(self, *args):
                yield {"delta": "partial"}
                raise OSError("connection dropped")
        async def run():
            with patch.object(server, "get_client", lambda: BrokenClient()):
                response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
                events=_parse_sse("".join([chunk async for chunk in response.body_iterator]))
            self.assertEqual(events[-1][0], "response.failed")
            self.assertNotIn("response.completed", [name for name, data in events])
            self.assertEqual([data["sequence_number"] for name, data in events], list(range(len(events))))
            rid=events[0][1]["response"]["id"]
            self.assertEqual((await server.retrieve_response(rid))["status"], "failed")
        asyncio.run(run())

    def test_stream_sends_final_text_missing_from_deltas(self):
        async def run():
            self.client.chunks=[{"delta": "hel"}, {"done": True, "answer": "hello"}]
            response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
            events=_parse_sse("".join([chunk async for chunk in response.body_iterator]))
            text="".join(data["delta"] for name, data in events if name == "response.output_text.delta")
            self.assertEqual(text, "hello")
            self.assertEqual(events[-1][1]["response"]["output_text"], text)
        asyncio.run(run())

    def test_stream_reasoning_delta_matches_done_text(self):
        async def run():
            self.client.chunks=[{"thinking": "one"}, {"thinking": "two"}, {"done": True, "answer": "ok"}]
            response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
            events=_parse_sse("".join([chunk async for chunk in response.body_iterator]))
            text="".join(data["delta"] for name, data in events if name == "response.reasoning_summary_text.delta")
            done=next(data["text"] for name, data in events if name == "response.reasoning_summary_text.done")
            self.assertEqual(text, done)
        asyncio.run(run())

    def test_non_background_response_cannot_be_cancelled(self):
        async def run():
            response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
            rid=next(iter(server._responses_store))
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                await server.cancel_response(rid)
            self.assertEqual(ctx.exception.status_code, 400)
            async for chunk in response.body_iterator:
                pass
        asyncio.run(run())

    def test_deleted_stream_does_not_resurrect_record(self):
        async def run():
            response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
            rid=next(iter(server._responses_store))
            await server.delete_response(rid)
            async for chunk in response.body_iterator:
                pass
            self.assertIsNone(server._responses_get(rid))
        asyncio.run(run())

    def test_previous_response_must_be_completed(self):
        server._responses_put({"id": "resp_failed", "status": "failed", "created_at": server.time.time()})
        with self.assertRaises(server.OpenAIAPIError) as ctx:
            server._responses_apply_previous([{"role": "user", "content": "Hi"}], "resp_failed", None)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_quota_exhaustion_does_not_silently_change_model(self):
        with patch.object(server, "_rate_limit", {"remaining_pro": 0}):
            with self.assertRaises(server.OpenAIAPIError) as ctx:
                server._responses_resolve_model("gpt", False)
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertEqual(server._responses_resolve_model("auto", False)[1], "auto")

    def test_stream_cleaner_handles_all_marker_split_boundaries(self):
        samples=[
            "  answer[123] done \n",
            "a<grok:render>hidden</grok:render>b",
            "a<grok:render id='x'/>b",
            "a<script>hidden<grok:</script>b",
            "<?xml version='1.0'?><response>visible</response>",
            "literal <table> and [unfinished",
        ]
        for text in samples:
            for split in range(len(text)+1):
                with self.subTest(text=text, split=split):
                    cleaner=server._ResponseStreamCleaner()
                    actual=cleaner.feed(text[:split])+cleaner.feed(text[split:])+cleaner.feed("", final=True)
                    self.assertEqual(actual, server._clean_response(text, strip=False))
            cleaner=server._ResponseStreamCleaner()
            actual="".join(cleaner.feed(char) for char in text)+cleaner.feed("", final=True)
            self.assertEqual(actual, server._clean_response(text, strip=False))

    def test_stream_split_markers_and_whitespace_match_completed_output(self):
        async def run():
            for text in ("  answer[12] done \n", "before<script>secret</script>after", "a<grok:x>hide</grok:x>b", "<?xml version='1.0'?><response>Hi</response>"):
                with self.subTest(text=text):
                    self.client.chunks=[{"delta": char} for char in text]+[{"done": True, "answer": text}]
                    response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
                    events=_parse_sse("".join([chunk async for chunk in response.body_iterator]))
                    self.assertEqual(events[-1][0], "response.completed")
                    actual="".join(data["delta"] for name, data in events if name == "response.output_text.delta")
                    self.assertEqual(actual, server._clean_response(text, strip=False))
                    self.assertEqual(actual, events[-1][1]["response"]["output_text"])
        asyncio.run(run())

    def test_stream_disconnect_closes_upstream_and_marks_cancelled(self):
        closed=[]
        class ResourceClient:
            async def search(self, *args):
                try:
                    yield {"delta": "partial"}
                    yield {"done": True, "answer": "partial"}
                finally:
                    closed.append(True)
        async def run():
            with patch.object(server, "get_client", lambda: ResourceClient()):
                response=await server.responses_api(FakeRequest({"model": "auto", "input": "Hi", "stream": True}))
                iterator=response.body_iterator
                async for chunk in iterator:
                    if "event: response.output_text.delta" in chunk:
                        break
                await iterator.aclose()
            self.assertEqual(closed, [True])
            rec=next(iter(server._responses_store.values()))
            self.assertEqual(rec["status"], "cancelled")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
