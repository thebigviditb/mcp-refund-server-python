#!/usr/bin/env bash
# Manual proof of the two evaluation-critical properties:
#   1. stdout carries nothing but JSON-RPC frames
#   2. malformed input comes back as standard JSON-RPC error codes
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-.venv/bin/python}"

requests=(
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"1.0.0"}}}'
  '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-10001"}}}'
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"cust-1"}}}'
  '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-10001","amount":"12.50","reason":"stringy amount"}}}'
  '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-10001","amount":25,"reason":"too short"}}}'
  '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"nope","arguments":{}}}'
  '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"trigger_refund","arguments":{"customer_id":"CUST-10001","amount":10,"reason":"Legitimate duplicate charge"}}}'
  '{"jsonrpc":"2.0","id":9,"method":"ping"}'
)

out=$(mktemp); err=$(mktemp)
# The trailing sleep holds the pipe open while the last responses are written.
# A real host keeps the connection open; closing stdin immediately after the
# final request races the transport's shutdown and can drop trailing frames.
{ printf '%s\n' "${requests[@]}"; sleep 1; } | "$PY" -m mcp_refund_server >"$out" 2>"$err" || true

echo "=== stdout (must be pure JSON-RPC) ==="
"$PY" - "$out" <<'PYEOF'
import json, sys
bad = 0
for n, line in enumerate(open(sys.argv[1]), 1):
    if not line.strip():
        continue
    try:
        msg = json.loads(line)
        assert msg["jsonrpc"] == "2.0"
    except Exception as exc:
        bad += 1
        print(f"  LINE {n}: NOT JSON-RPC -> {line[:120]!r} ({exc})")
        continue
    if "error" in msg:
        print(f"  id={msg['id']}  error {msg['error']['code']}: {msg['error']['message'][:90]}")
    else:
        print(f"  id={msg.get('id')}  ok")
print("\nPASS: stdout is pure JSON-RPC" if bad == 0 else f"\nFAIL: {bad} non-JSON-RPC line(s)")
PYEOF

echo
echo "=== stderr (all diagnostics land here) ==="
head -6 "$err"
rm -f "$out" "$err"
