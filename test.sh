#!/usr/bin/env bash
BASE="${1:-http://localhost:8892}"
KEY=$(grep PPLX_PROXY_API_KEY ~/pplx-proxy/.env 2>/dev/null | head -1 | cut -d= -f2)

echo "=== pplx-proxy smoke test @ $BASE ==="

echo "--- Health ---"
curl -s "$BASE/health" | python3 -m json.tool 2>/dev/null

echo "--- Models ---"
curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models" | python3 -c "import sys,json; [print(f'  {m[\"id\"]}') for m in json.load(sys.stdin)['data']]" 2>/dev/null

echo "--- Chat (non-streaming) ---"
curl -s "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"What is 2+2? Answer in one word."}],"stream":false}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  {d[\"choices\"][0][\"finish_reason\"]}: {d[\"choices\"][0][\"message\"][\"content\"][:60]}')" 2>/dev/null

echo "--- Chat (streaming) ---"
timeout 15 curl -sN "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"sonnet","messages":[{"role":"user","content":"Say hello in 3 words"}],"stream":true}' 2>/dev/null \
  | head -8

echo ""
echo "--- Debug page ---"
echo "  Open $BASE/chat to test interactively"

echo ""
echo "=== Done ==="
