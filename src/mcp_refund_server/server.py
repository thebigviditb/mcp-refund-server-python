"""MCP server exposing two customer-support tools over stdio.

    get_customer_record  -- look up a customer by CUST-XXXXX id
    trigger_refund       -- raise a refund against that customer

Error mapping
-------------
Protocol- and contract-level faults are JSON-RPC errors, because the request
itself was not well formed:

    -32601 METHOD_NOT_FOUND   unknown tool name
    -32602 INVALID_PARAMS     arguments failed schema validation
    -32603 INTERNAL_ERROR     unexpected exception

Domain faults (no such customer, balance too low) are *successful* responses
carrying ``is_error=True``. That is what the MCP spec prescribes: the call
executed correctly, and the model needs to read the outcome and adjust rather
than have its turn aborted by a protocol error.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Union

import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ValidationError

from .logging_setup import configure_logging, log
from .schemas import (
    GET_CUSTOMER_RECORD_SCHEMA,
    TRIGGER_REFUND_SCHEMA,
    GetCustomerRecordInput,
    TriggerRefundInput,
)
from .store import (
    RefundAccepted,
    create_refund,
    get_customer,
    record_as_dict,
    refund_as_dict,
)
from .stdout_guard import stdout_is_diverted

SERVER_NAME = "mcp-refund-server"
SERVER_VERSION = "1.0.0"

logger = logging.getLogger("mcp_refund_server")


TOOLS = [
    types.Tool(
        name="get_customer_record",
        title="Get customer record",
        description=(
            "Fetch the billing record for a customer. `customer_id` must be formatted "
            "as CUST-XXXXX, where XXXXX is exactly five uppercase letters or digits."
        ),
        input_schema=GET_CUSTOMER_RECORD_SCHEMA,
        annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    ),
    types.Tool(
        name="trigger_refund",
        title="Trigger refund",
        description=(
            "Raise a pending refund against a customer's refundable balance. Requires a "
            "positive `amount` with at most two decimal places and a `reason` of at least "
            "10 characters. This writes to the billing system and is not reversible."
        ),
        input_schema=TRIGGER_REFUND_SCHEMA,
        annotations=types.ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
    ),
]

TOOL_NAMES = [tool.name for tool in TOOLS]


# --------------------------------------------------------------------------- #
# Result and error helpers
# --------------------------------------------------------------------------- #

def _ok(payload: Any) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]
    )


def _domain_error(payload: dict) -> types.CallToolResult:
    """A call that ran correctly and produced a business-rule refusal."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        is_error=True,
    )


def _invalid_params(tool_name: str, error: ValidationError) -> MCPError:
    """Render a Pydantic ValidationError as a standards-compliant -32602."""
    issues = [
        {
            "field": ".".join(str(p) for p in err["loc"]) or "(root)",
            "code": err["type"],
            "message": err["msg"],
        }
        for err in error.errors()
    ]
    summary = "; ".join(f"{i['field']}: {i['message']}" for i in issues)

    log(logger, logging.WARNING, "input validation failed", tool=tool_name, issues=issues)

    return MCPError(
        code=types.INVALID_PARAMS,
        message=f"Invalid arguments for {tool_name}: {summary}",
        data={"tool": tool_name, "issues": issues},
    )


def _parse(model: type[BaseModel], tool_name: str, arguments: Union[dict, None]) -> Any:
    try:
        return model.model_validate(arguments or {})
    except ValidationError as exc:
        raise _invalid_params(tool_name, exc) from None


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #

def handle_get_customer_record(arguments: Union[dict, None]) -> types.CallToolResult:
    args: GetCustomerRecordInput = _parse(GetCustomerRecordInput, "get_customer_record", arguments)

    record = get_customer(args.customer_id)
    if record is None:
        log(logger, logging.INFO, "customer lookup miss", customer_id=args.customer_id)
        return _domain_error(
            {
                "error": "customer_not_found",
                "customer_id": args.customer_id,
                "message": f"No customer exists with id {args.customer_id}.",
            }
        )

    log(logger, logging.INFO, "customer lookup hit", customer_id=args.customer_id)
    return _ok(record_as_dict(record))


def handle_trigger_refund(arguments: Union[dict, None]) -> types.CallToolResult:
    args: TriggerRefundInput = _parse(TriggerRefundInput, "trigger_refund", arguments)

    outcome = create_refund(args.customer_id, args.amount, args.reason)

    if not isinstance(outcome, RefundAccepted):
        log(
            logger,
            logging.INFO,
            "refund rejected",
            customer_id=args.customer_id,
            amount=args.amount,
            reason=outcome.kind,
        )
        messages = {
            "unknown_customer": f"No customer exists with id {args.customer_id}.",
            "account_not_active": (
                f"Refunds are not permitted on a {outcome.detail.get('status')} account."
            ),
            "insufficient_balance": (
                "Refund exceeds the refundable balance of "
                f"{outcome.detail.get('refundable_balance')}."
            ),
        }
        error_codes = {
            "unknown_customer": "customer_not_found",
            "account_not_active": "account_not_active",
            "insufficient_balance": "insufficient_refundable_balance",
        }
        return _domain_error(
            {
                "error": error_codes[outcome.kind],
                "customer_id": args.customer_id,
                "message": messages[outcome.kind],
                **outcome.detail,
            }
        )

    log(
        logger,
        logging.INFO,
        "refund created",
        customer_id=args.customer_id,
        refund_id=outcome.refund.refund_id,
        amount=outcome.refund.amount,
    )
    return _ok({**refund_as_dict(outcome.refund), "remaining_balance": outcome.remaining_balance})


# --------------------------------------------------------------------------- #
# Request handlers
# --------------------------------------------------------------------------- #

async def on_list_tools(
    ctx: ServerRequestContext[Any], params: Union[types.PaginatedRequestParams, None]
) -> types.ListToolsResult:
    return types.ListToolsResult(tools=TOOLS)


async def on_call_tool(
    ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    name = params.name
    log(logger, logging.DEBUG, "tools/call received", tool=name)

    handlers = {
        "get_customer_record": handle_get_customer_record,
        "trigger_refund": handle_trigger_refund,
    }
    handler = handlers.get(name)
    if handler is None:
        raise MCPError(
            code=types.METHOD_NOT_FOUND,
            message=f"Unknown tool: {name}",
            data={"available_tools": TOOL_NAMES},
        )

    try:
        return handler(params.arguments)
    except MCPError:
        # Already carries the right code; let the runner serialise it.
        raise
    except Exception:
        # Never leak internals onto the wire, but keep the traceback on stderr.
        logger.exception("unhandled exception in tool handler", extra={"fields": {"tool": name}})
        raise MCPError(
            code=types.INTERNAL_ERROR, message=f"Internal error executing {name}"
        ) from None


def build_server() -> Server:
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve() -> None:
    configure_logging()
    server = build_server()

    # No `stdout=` argument, deliberately: passing one would opt out of the
    # transport's claim on fd 1, which is what keeps stray prints off the wire.
    # See stdout_guard for the full explanation.
    async with stdio_server() as (read_stream, write_stream):
        log(
            logger,
            logging.INFO,
            "server ready on stdio",
            name=SERVER_NAME,
            version=SERVER_VERSION,
            tools=TOOL_NAMES,
            stdout_diverted=stdout_is_diverted(),
        )
        if not stdout_is_diverted():
            log(
                logger,
                logging.WARNING,
                "fd 1 was not diverted to stderr; a stray print() could corrupt the "
                "JSON-RPC stream on this platform",
            )
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=None, experimental_capabilities={}
                ),
            ),
        )
