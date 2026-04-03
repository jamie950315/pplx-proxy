#!/usr/bin/env bash
# Usage: ./inject_cookie.sh <session-token>
# Get your token: perplexity.ai → F12 → Application → Cookies → next-auth.session-token

TOKEN="$1"
if [ -z "$TOKEN" ]; then
    echo "Usage: ./inject_cookie.sh <next-auth.session-token value>"
    echo ""
    echo "How to get it:"
    echo "  1. Open https://www.perplexity.ai in your browser (logged in)"
    echo "  2. F12 → Application → Cookies → www.perplexity.ai"
    echo "  3. Copy the value of 'next-auth.session-token'"
    echo "  4. Run: ./inject_cookie.sh <paste-value-here>"
    exit 1
fi

COOKIE_FILE="$(dirname "$0")/.cookie_cache.json"
cat > "$COOKIE_FILE" << JSONEOF
{
  "cookies": {
    "next-auth.session-token": "${TOKEN}"
  },
  "timestamp": $(date +%s)
}
JSONEOF

echo "Cookie saved to $COOKIE_FILE"
echo "Restarting pplx-proxy..."
sudo systemctl restart pplx-proxy
sleep 2
sudo systemctl status pplx-proxy --no-pager | head -5
