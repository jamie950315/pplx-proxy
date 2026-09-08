"""Live smoke checks. Any HTTP, JSON, or incomplete-stream failure exits nonzero."""
import json
import os
from pathlib import Path
import sys
import urllib.request

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).with_name(".env"))
    base=sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8892"
    headers={"Content-Type": "application/json", "User-Agent": "pplx-proxy-smoke/1.0"}
    key=os.environ.get("PPLX_PROXY_API_KEY", "")
    if key:
        headers["Authorization"]="Bearer " + key

    def request(path, body=None):
        payload=None if body is None else json.dumps(body).encode()
        return urllib.request.urlopen(
            urllib.request.Request(base + path, data=payload, headers=headers),
            timeout=120,
        )

    def get_json(path, body=None):
        with request(path, body) as response:
            return json.load(response)

    health=get_json("/health")
    if health.get("status") != "ok":
        raise RuntimeError("Health check did not report ok")
    print("Health: PASS", flush=True)
    models=get_json("/v1/models")
    if not models.get("data"):
        raise RuntimeError("Model list is empty")
    print(f"Models: PASS ({len(models['data'])})", flush=True)
    body={"model": "auto", "messages": [{"role": "user", "content": "What is 2+2? Answer in one word."}]}
    chat=get_json("/v1/chat/completions", {**body, "stream": False})
    choice=chat["choices"][0]
    if choice.get("finish_reason") != "stop" or not choice["message"].get("content"):
        raise RuntimeError("Chat response is incomplete or empty")
    print("Chat: PASS", flush=True)
    got_done=False
    got_content=False
    got_finish=False
    with request("/v1/chat/completions", {**body, "stream": True}) as response:
        for raw in response:
            line=raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            value=line[5:].strip()
            if value == "[DONE]":
                got_done=True
                break
            chunk=json.loads(value)
            if "error" in chunk:
                raise RuntimeError(f"Stream error: {chunk['error']}")
            for choice in chunk.get("choices", []):
                got_content |= bool(choice.get("delta", {}).get("content"))
                got_finish |= choice.get("finish_reason") == "stop"
    if not (got_done and got_content and got_finish):
        raise RuntimeError("Stream is incomplete: expected content, stop, and [DONE]")
    print("Streaming chat: PASS", flush=True)
    response=get_json("/v1/responses", {"model": "auto", "input": "What is 2+2? Answer in one word.", "store": False})
    content="".join(
        part.get("text", "")
        for item in response.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if response.get("status") != "completed" or not response.get("id") or not content:
        raise RuntimeError("Responses result is incomplete or empty")
    print("Responses: PASS", flush=True)
    print(f"All smoke checks passed. Debug page: {base}/chat")


if __name__ == "__main__":
    main()
