#!/usr/bin/env bash
# Usage: ./inject_cookie.sh (hidden prompt), or pipe the session token on stdin.
# Get __Secure-next-auth.session-token from your logged-in browser cookies.
set -euo pipefail
umask 077

if (( $# > 1 )); then
    echo "Usage: ./inject_cookie.sh [session-token]" >&2
    exit 1
fi
if (( $# == 1 )); then
    TOKEN=$1
elif [[ -t 0 ]]; then
    read -r -s -p 'Perplexity session token: ' TOKEN
    echo
else
    TOKEN=$(cat)
fi
if [[ -z "$TOKEN" ]]; then
    echo "Session token cannot be empty." >&2
    exit 1
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
PYTHON="${PYTHON:-$SCRIPT_DIR/venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then PYTHON=python3; fi
# The running endpoint validates and atomically persists the session in its
# configured DATA_DIR. No restart or direct cache overwrite is needed.
printf '%s' "$TOKEN" | "$PYTHON" -c '
import json, os, sys, urllib.error, urllib.request
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(sys.argv[1]) / ".env")
port=int(os.environ.get("PPLX_PROXY_PORT", "8892"))
headers={"Content-Type": "application/json"}
key=os.environ.get("PPLX_PROXY_API_KEY", "")
if key:
    headers["Authorization"]="Bearer " + key
request=urllib.request.Request(
    f"http://127.0.0.1:{port}/admin/refresh-cookie",
    data=json.dumps({"session_token": sys.stdin.read()}).encode(),
    headers=headers, method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        result=json.load(response)
except urllib.error.HTTPError as error:
    sys.exit(f"Cookie update rejected (HTTP {error.code}); existing session retained.")
except urllib.error.URLError:
    sys.exit("Cannot reach pplx-proxy; ensure the service is running.")
if result.get("status") != "ok":
    sys.exit("Cookie update failed: server did not confirm success.")
print("Cookie validated and saved. The running service is using the updated session.")
' "$SCRIPT_DIR"
