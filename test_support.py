"""Support-script tests without contacting Perplexity or using real credentials."""
import contextlib
import io
import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import smoke_test


class SmokeTestChecks(unittest.TestCase):
    def responses(self, stream=None):
        chunk={"choices": [{"delta": {"content": "four"}, "finish_reason": "stop"}]}
        stream=stream if stream is not None else b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
        values=[
            {"status": "ok"},
            {"data": [{"id": "auto"}]},
            {"choices": [{"message": {"content": "four"}, "finish_reason": "stop"}]},
            stream,
            {"id": "resp-test", "status": "completed", "output": [{"content": [{"type": "output_text", "text": "four"}]}]},
        ]
        return [io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode()) for value in values]

    def run_smoke(self, responses):
        with patch.object(sys, "argv", ["smoke_test.py", "http://localhost:1"]), patch.object(smoke_test.urllib.request, "urlopen", side_effect=responses), contextlib.redirect_stdout(io.StringIO()):
            smoke_test.main()

    def test_complete_stream_passes(self):
        self.run_smoke(self.responses())

    def test_interrupted_stream_fails(self):
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.run_smoke(self.responses(b'data: {"choices":[]}\n\n'))

    def test_stream_error_fails(self):
        with self.assertRaisesRegex(RuntimeError, "Stream error"):
            self.run_smoke(self.responses(b'data: {"error":{"message":"upstream failed"}}\n\n'))

    def test_http_error_propagates(self):
        error=smoke_test.urllib.error.HTTPError("http://localhost", 401, "Unauthorized", {}, None)
        with self.assertRaises(smoke_test.urllib.error.HTTPError):
            self.run_smoke([error])


class CookieScriptChecks(unittest.TestCase):
    def test_validated_update_and_rejection(self):
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                valid=received[-1]["session_token"] == 'test-token-with-"quote'
                self.send_response(200 if valid else 401)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}' if valid else b'{"detail":"invalid"}')
            def log_message(self, *_args):
                pass
        server=ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread=threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            import os
            env={**os.environ, "PYTHON": sys.executable, "PPLX_PROXY_PORT": str(server.server_port), "PPLX_PROXY_API_KEY": "test-key"}
            for token,expected in [('test-token-with-"quote', 0), ('invalid-test-token', 1)]:
                result=subprocess.run(["bash", str(Path(__file__).with_name("inject_cookie.sh"))], input=token, text=True, capture_output=True, env=env, timeout=10)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn(token, result.stdout + result.stderr)
            self.assertEqual(received, [{"session_token": 'test-token-with-"quote'}, {"session_token": "invalid-test-token"}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
