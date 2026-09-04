"""End-to-end protocol tests against a real server subprocess."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import PROJECT_ROOT, payload

INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def test_advertises_both_tools_with_published_schemas(client):
    result = client.send("tools/list", {})["result"]
    assert sorted(t["name"] for t in result["tools"]) == [
        "get_customer_record",
        "trigger_refund",
    ]

    refund = next(t for t in result["tools"] if t["name"] == "trigger_refund")
    schema = refund["inputSchema"]
    assert schema["type"] == "object"
    assert sorted(schema["required"]) == ["amount", "customer_id", "reason"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["customer_id"]["pattern"] == r"^CUST-[A-Z0-9]{5}$"
    assert schema["properties"]["amount"]["exclusiveMinimum"] == 0
    assert schema["properties"]["reason"]["minLength"] == 10
    assert schema["properties"]["reason"]["maxLength"] == 500


def test_refund_tool_is_annotated_destructive(client):
    result = client.send("tools/list", {})["result"]
    refund = next(t for t in result["tools"] if t["name"] == "trigger_refund")
    lookup = next(t for t in result["tools"] if t["name"] == "get_customer_record")
    assert refund["annotations"]["destructiveHint"] is True
    assert lookup["annotations"]["readOnlyHint"] is True


# --------------------------------------------------------------------------- #
# get_customer_record
# --------------------------------------------------------------------------- #

def test_returns_record_for_valid_id(client):
    result = client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})["result"]
    assert not result.get("isError")
    assert payload(result)["name"] == "Ada Lovelace"


def test_accepts_digits_and_uppercase_letters_in_suffix(client):
    result = client.call_tool("get_customer_record", {"customer_id": "CUST-2A7B9"})["result"]
    assert payload(result)["plan"] == "pro"


def test_unknown_id_is_a_domain_error_not_a_protocol_error(client):
    response = client.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
    assert "error" not in response, "a well-formed lookup miss must not be a JSON-RPC error"
    assert response["result"]["isError"] is True
    assert payload(response["result"])["error"] == "customer_not_found"


# --------------------------------------------------------------------------- #
# customer_id validation -> -32602
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "label,customer_id",
    [
        ("lowercase suffix", "cust-10001"),
        ("mixed-case prefix", "Cust-10001"),
        ("too short", "CUST-1000"),
        ("too long", "CUST-100011"),
        ("missing prefix", "10001"),
        ("wrong separator", "CUST_10001"),
        ("leading whitespace", " CUST-10001"),
        ("trailing newline", "CUST-10001\n"),
        ("embedded in text", "id is CUST-10001"),
        ("empty string", ""),
        ("unicode digits", "CUST-１０００１"),
        ("lowercase letters in suffix", "CUST-abcde"),
    ],
)
def test_rejects_malformed_customer_id(client, label, customer_id):
    response = client.call_tool("get_customer_record", {"customer_id": customer_id})
    assert "result" not in response, f"{label} should not succeed"
    assert response["error"]["code"] == INVALID_PARAMS
    assert response["error"]["data"]["issues"][0]["field"] == "customer_id"


@pytest.mark.parametrize("value", [10001, None, True, ["CUST-10001"], {"id": "CUST-10001"}])
def test_rejects_non_string_customer_id(client, value):
    response = client.call_tool("get_customer_record", {"customer_id": value})
    assert response["error"]["code"] == INVALID_PARAMS


def test_rejects_missing_customer_id(client):
    assert client.call_tool("get_customer_record", {})["error"]["code"] == INVALID_PARAMS


def test_rejects_unknown_extra_properties(client):
    response = client.call_tool(
        "get_customer_record", {"customer_id": "CUST-10001", "admin_override": True}
    )
    assert response["error"]["code"] == INVALID_PARAMS
    assert any(i["code"] == "extra_forbidden" for i in response["error"]["data"]["issues"])


def test_rejects_null_arguments_object(client):
    response = client.send("tools/call", {"name": "get_customer_record", "arguments": None})
    assert response["error"]["code"] == INVALID_PARAMS


def test_rejects_array_in_place_of_arguments(client):
    response = client.send(
        "tools/call", {"name": "get_customer_record", "arguments": ["CUST-10001"]}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_rejects_missing_tool_name(client):
    response = client.send("tools/call", {"arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS


# --------------------------------------------------------------------------- #
# trigger_refund
# --------------------------------------------------------------------------- #

def test_creates_pending_refund_and_debits_balance(client):
    before = payload(
        client.call_tool("get_customer_record", {"customer_id": "CUST-10001"})["result"]
    )["refundable_balance"]

    result = client.call_tool(
        "trigger_refund",
        {
            "customer_id": "CUST-10001",
            "amount": 250.50,
            "reason": "Duplicate charge on the March invoice",
        },
    )["result"]
    body = payload(result)
    assert body["refund_id"].startswith("RFND-")
    assert body["amount"] == 250.50
    assert body["status"] == "pending"
    # Asserted as a delta so the test does not depend on which other tests ran.
    assert body["remaining_balance"] == pytest.approx(before - 250.50)


def test_trims_the_reason_before_storing_it(client):
    result = client.call_tool(
        "trigger_refund",
        {
            "customer_id": "CUST-10001",
            "amount": 0.01,
            "reason": "   Goodwill credit for the outage   ",
        },
    )["result"]
    assert payload(result)["reason"] == "Goodwill credit for the outage"


def test_over_balance_refund_is_a_domain_error(client):
    response = client.call_tool(
        "trigger_refund",
        {
            "customer_id": "CUST-2A7B9",
            "amount": 5000,
            "reason": "Attempting to exceed the refundable balance",
        },
    )
    assert "error" not in response
    assert response["result"]["isError"] is True
    assert payload(response["result"])["error"] == "insufficient_refundable_balance"


def test_refuses_refund_on_suspended_account(client):
    response = client.call_tool(
        "trigger_refund",
        {
            "customer_id": "CUST-30003",
            "amount": 10,
            "reason": "Refund against a suspended account",
        },
    )
    assert response["result"]["isError"] is True
    assert payload(response["result"])["error"] == "account_not_active"


def test_refund_for_unknown_customer_is_a_domain_error(client):
    response = client.call_tool(
        "trigger_refund",
        {
            "customer_id": "CUST-77777",
            "amount": 10,
            "reason": "Refund for a customer that does not exist",
        },
    )
    assert response["result"]["isError"] is True
    assert payload(response["result"])["error"] == "customer_not_found"


# --------------------------------------------------------------------------- #
# amount and reason validation -> -32602
# --------------------------------------------------------------------------- #

BASE = {"customer_id": "CUST-10001", "reason": "A sufficiently long refund reason"}


@pytest.mark.parametrize(
    "label,amount",
    [
        ("zero", 0),
        ("negative", -10),
        ("negative cents", -0.01),
        ("numeric string", "12.50"),
        ("three decimal places", 10.005),
        ("above the ceiling", 10_000.01),
        ("null", None),
        ("boolean", True),
        ("list", [10]),
        ("dict", {"value": 10}),
        ("NaN", float("nan")),
        ("Infinity", float("inf")),
        ("-Infinity", float("-inf")),
    ],
)
def test_rejects_invalid_amount(client, label, amount):
    # NaN/Infinity are non-standard JSON but Python's json module emits and
    # accepts them, so they genuinely can arrive over the wire.
    response = client.call_tool("trigger_refund", {**BASE, "amount": amount})
    assert "result" not in response, f"{label} should not succeed"
    assert response["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("amount", [0.01, 9999.99, 100])
def test_boundary_amounts_clear_validation(client, amount):
    response = client.call_tool("trigger_refund", {**BASE, "amount": amount})
    # Some of these exceed the balance; they must fail on domain rules, if at
    # all, never on schema validation.
    assert "error" not in response, f"{amount} should pass schema validation"


@pytest.mark.parametrize(
    "label,reason",
    [
        ("too short", "too short"),
        ("exactly 9 characters", "123456789"),
        ("whitespace padded to length", "   short   "),
        ("only whitespace", "              "),
        ("empty", ""),
        ("non-string", 12345),
        ("null", None),
        ("over max length", "x" * 501),
    ],
)
def test_rejects_invalid_reason(client, label, reason):
    response = client.call_tool(
        "trigger_refund", {"customer_id": "CUST-10001", "amount": 5, "reason": reason}
    )
    assert "result" not in response, f"{label} should not succeed"
    assert response["error"]["code"] == INVALID_PARAMS


def test_accepts_exactly_ten_characters(client):
    response = client.call_tool(
        "trigger_refund", {"customer_id": "CUST-10001", "amount": 1, "reason": "1234567890"}
    )
    assert "error" not in response


def test_reports_every_failing_field_at_once(client):
    response = client.call_tool(
        "trigger_refund", {"customer_id": "nope", "amount": -1, "reason": "short"}
    )
    assert response["error"]["code"] == INVALID_PARAMS
    fields = sorted({i["field"] for i in response["error"]["data"]["issues"]})
    assert fields == ["amount", "customer_id", "reason"]


# --------------------------------------------------------------------------- #
# Protocol compliance
# --------------------------------------------------------------------------- #

def test_unknown_tool_is_method_not_found(client):
    response = client.call_tool("delete_everything", {})
    assert response["error"]["code"] == METHOD_NOT_FOUND
    assert sorted(response["error"]["data"]["available_tools"]) == [
        "get_customer_record",
        "trigger_refund",
    ]


def test_unknown_method_is_method_not_found(client):
    assert client.send("resources/list", {})["error"]["code"] == METHOD_NOT_FOUND


def test_responses_echo_jsonrpc_version_and_id(client):
    response = client.send("tools/list", {})
    assert response["jsonrpc"] == "2.0"
    assert isinstance(response["id"], int)


def test_survives_a_malformed_frame_and_keeps_serving(client):
    client.send_raw("this is not json\n")
    client.send_raw('{"jsonrpc":"2.0","id":\n')  # truncated frame
    assert len(client.send("tools/list", {})["result"]["tools"]) == 2


# --------------------------------------------------------------------------- #
# STDIO isolation
# --------------------------------------------------------------------------- #

def test_every_line_on_stdout_is_a_jsonrpc_message(client):
    assert client.stdout_lines, "expected traffic on stdout"
    for line in client.stdout_lines:
        parsed = json.loads(line)  # raises if anything else got in
        assert parsed["jsonrpc"] == "2.0", f"missing JSON-RPC envelope: {line}"


def test_diagnostics_went_to_stderr(client):
    assert "server ready on stdio" in client.stderr
    assert '"level": "info"' in client.stderr


def test_stderr_never_contains_a_response_envelope(client):
    assert '"result": {"tools"' not in client.stderr


def test_transport_diverts_print_and_direct_fd_writes_to_stderr():
    """The kernel-level guarantee: while serving, fd 1 does not reach the wire.

    Runs a real `stdio_server()` session, makes the noisiest writes a handler
    could plausibly make, and checks that the pipe still carries exactly one
    JSON-RPC frame -- the one written through the transport.
    """
    script = """
import os, sys, anyio
sys.path.insert(0, "src")
import mcp_types as types
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp_refund_server.stdout_guard import stdout_is_diverted

async def main():
    # move_on_after: stdio_server serves until stdin closes, and this script
    # never wants to read a request -- it only needs the claim to be active.
    with anyio.move_on_after(10):
        async with stdio_server() as (read_stream, write_stream):
            assert stdout_is_diverted(), "fd 1 was not diverted"

            print("chatty debug line")                    # print()
            sys.stdout.write("direct sys.stdout write\\n") # sys.stdout
            sys.stdout.flush()
            os.write(1, b"raw fd 1 write\\n")              # bypasses Python
            os.system("echo subprocess inherited fd 1")   # a child process

            # The wire itself still works, through the transport.
            await write_stream.send(SessionMessage(
                types.JSONRPCResponse(jsonrpc="2.0", id=1, result={})
            ))
            await anyio.sleep(0.5)   # let the writer task flush the frame
            await write_stream.aclose()  # ends the writer task, so we exit

anyio.run(main)
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    assert [json.loads(l)["jsonrpc"] for l in lines] == ["2.0"], (
        f"stdout was not pure JSON-RPC: {proc.stdout!r}"
    )

    for leak in (
        "chatty debug line",
        "direct sys.stdout write",
        "raw fd 1 write",
        "subprocess inherited fd 1",
    ):
        assert leak not in proc.stdout, f"{leak!r} leaked onto stdout"
        assert leak in proc.stderr, f"{leak!r} did not reach stderr"


def test_library_logging_goes_to_stderr():
    """A dependency calling logging.info() must not reach the JSON-RPC channel."""
    script = """
import logging, sys
sys.path.insert(0, "src")
from mcp_refund_server.logging_setup import configure_logging

configure_logging()
logging.getLogger("some.third.party").warning("noisy dependency")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "noisy dependency" not in proc.stdout
    assert "noisy dependency" in proc.stderr


def test_server_reports_the_diversion_at_startup(client):
    """The running server self-checks that the claim actually took effect."""
    assert '"stdout_diverted": true' in client.stderr
