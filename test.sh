#!/usr/bin/env bash
BASE="${1:-http://localhost:8892}"
KEY=$(grep PPLX_PROXY_API_KEY ~/pplx-proxy/.env | head -1 | cut -d= -f2)

echo "=== pplx-proxy test @ $BASE ==="

echo "--- Health ---"
curl -s "$BASE/health" | python3 -m json.tool 2>/dev/null

echo "--- Models ---"
curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models" | python3 -m json.tool 2>/dev/null | head -20

echo "--- Chat (non-streaming, pplx-auto) ---"
curl -s "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"pplx-auto","messages":[{"role":"user","content":"What is 2+2?"}],"stream":false}' \
  | python3 -m json.tool 2>/dev/null

echo "--- Chat (streaming, pplx-pro) ---"
curl -sN "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -d '{"model":"pplx-pro","messages":[{"role":"user","content":"Latest TSMC news 2026"}],"stream":true}' \
  | head -20

echo ""
echo "=== Done ==="
