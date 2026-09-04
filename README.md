# mcp-refund-server

An MCP server over stdio exposing two customer-support tools, built on the
official Python SDK (`mcp`) with Pydantic for input validation.

| Tool | Inputs |
| --- | --- |
| `get_customer_record` | `customer_id` — `CUST-XXXXX` |
| `trigger_refund` | `customer_id`, `amount` (positive, ≤ 2dp), `reason` (≥ 10 chars) |

## Running it

```bash
uv venv --python 3.10
uv pip install -e ".[dev]"

.venv/bin/python -m mcp_refund_server   # speaks JSON-RPC on stdin/stdout
.venv/bin/python -m pytest -q           # 68 tests
./verify_stdout.sh                      # manual stdout-purity + error-code proof
```

Register it with a host (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "refunds": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "mcp_refund_server"],
      "env": { "MCP_LOG_LEVEL": "INFO" }
    }
  }
}
```

## STDIO isolation

`stdout` carries newline-framed JSON-RPC and nothing else. Two independent
things make that true:

1. **The transport claims fd 1.** `stdio_server()` duplicates stdout onto a
   private descriptor, serves the wire from that, and points fd 1 itself at
   stderr (fd 0 at `/dev/null`). While the server runs, `print()`,
   `sys.stdout.write`, a C extension writing to fd 1, and even a subprocess
   inheriting the descriptor all land on stderr. This is a kernel-level
   guarantee, not a convention, so it covers code that has never heard of this
   server.

   The corollary is the one real footgun, and it is why
   [`stdout_guard.py`](src/mcp_refund_server/stdout_guard.py) exists as
   documentation: **passing an explicit `stdout=` to `stdio_server()` opts out
   of the claim.** `serve()` deliberately passes nothing, and no code here may
   rebind `sys.stdout` before the transport starts — the claim inspects
   `sys.stdout` to locate fd 1.

2. **Diagnostics go somewhere useful.** `logging_setup` points the *root*
   logger at stderr as line-delimited JSON, so a chatty dependency is captured
   rather than silently discarded. `MCP_LOG_LEVEL` controls verbosity.

`serve()` calls `stdout_is_diverted()` at startup and logs a warning if the
claim did not take effect (it is best-effort on exotic platforms). The test
suite asserts the property from the outside: every byte the server process
writes to stdout is parsed and checked for a `jsonrpc: "2.0"` envelope, and a
dedicated test makes the four noisiest writes a handler could make and confirms
the pipe still carries exactly one frame.

## Error mapping

Protocol- and contract-level faults are JSON-RPC errors, because the request
itself was not well formed:

| Code | Condition |
| --- | --- |
| `-32601` MethodNotFound | unknown tool name, unknown method |
| `-32602` InvalidParams | arguments failed schema validation, malformed `params` |
| `-32603` InternalError | unexpected exception (details stay on stderr) |

Every `-32602` carries structured `data` so a client can repair the call
without parsing prose:

```json
{
  "code": -32602,
  "message": "Invalid arguments for trigger_refund: amount: Input should be a valid number",
  "data": {
    "tool": "trigger_refund",
    "issues": [{"field": "amount", "code": "float_type", "message": "Input should be a valid number"}]
  }
}
```

All failing fields are reported in one response rather than one per round trip.

**Domain faults are not protocol errors.** No such customer, a suspended
account, or a refund exceeding the refundable balance return a *successful*
response with `isError: true` and a machine-readable body. That is what the MCP
spec prescribes: the call executed correctly, and the model should read the
outcome and adjust rather than have its turn aborted. A well-formed lookup for
a customer that does not exist is a fact about the world, not a broken request.

## Validation

Pydantic models are the single source of truth: the same model produces the
JSON Schema published in `tools/list` and validates arguments on `tools/call`,
so what a client is told and what the server enforces cannot drift apart.

Models run in **strict mode** with `extra="forbid"`. Without it Pydantic would
coerce `"250.50"` into a float and accept `True` as a number — silently
papering over client bugs that are far better surfaced as `-32602`.

- **`customer_id`** — `^CUST-[A-Z0-9]{5}$`, anchored at both ends, so
  `"id is CUST-10001"`, `"CUST-10001\n"`, `"cust-10001"` and `"CUST-100011"`
  all fail.
- **`amount`** — must be a JSON number (not a numeric string), `> 0`,
  `≤ 10000`, and at most 2 decimal places; sub-cent precision is rejected
  rather than silently rounded. `allow_inf_nan=False` is not paranoia: Python's
  `json` module both emits and accepts the non-standard `NaN` and `Infinity`
  literals, so they genuinely can arrive over the wire.
- **`reason`** — `minLength` 10 is declared on the type so it appears in the
  published schema; a validator additionally requires 10 characters *after*
  trimming, closing the `"          "` loophole. The trimmed value is what gets
  stored, normalised once at the boundary.
- Refund arithmetic is done in integer cents. Floating-point drift has no place
  in money.

## Layout

```
src/mcp_refund_server/
  server.py         protocol wiring, handlers, error mapping
  schemas.py        Pydantic input contracts + published JSON Schema
  store.py          in-memory billing system (the only module to replace)
  logging_setup.py  stderr-only structured logging
  stdout_guard.py   stdout isolation: the guarantee and its one footgun
tests/
  conftest.py       JSON-RPC-over-stdio client that drives a real subprocess
  test_protocol.py  68 tests: discovery, validation, error codes, isolation
```

Tests spawn the actual server process and speak JSON-RPC to it over pipes —
no in-process shortcuts — so what they verify is what a host would see.

## Notes

- `store.py` is in-memory and process-local; replacing it with real HTTP calls
  requires no change to the protocol layer.
- `trigger_refund` is annotated `destructiveHint: true`, `get_customer_record`
  `readOnlyHint: true`, so hosts can gate the write behind confirmation.
- Closing stdin immediately after a final request races the SDK's transport
  shutdown and can drop trailing response frames. This is upstream behaviour,
  reproducible without this server's code; real hosts hold the pipe open.
  `verify_stdout.sh` keeps it open briefly for that reason.
