"""Input contracts.

Pydantic is the single source of truth: the same model produces the JSON Schema
published in ``tools/list`` and validates the arguments on ``tools/call``, so
what a client is told and what the server enforces cannot drift apart.

The models run in **strict mode**, which is the important choice here. Without
it Pydantic would happily coerce ``"250.50"`` into a float and ``1`` into
``True``, silently papering over client bugs that are much better surfaced as
an ``-32602 Invalid params`` response.
"""

from __future__ import annotations

import math
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

#: ``CUST-`` followed by exactly five uppercase alphanumerics. Anchored at both
#: ends so ``"id is CUST-10001"`` and ``"CUST-10001\n"`` both fail.
CUSTOMER_ID_PATTERN = r"^CUST-[A-Z0-9]{5}$"

MAX_REFUND_AMOUNT = 10_000.0

CustomerId = Annotated[
    str,
    StringConstraints(pattern=CUSTOMER_ID_PATTERN),
    Field(description="Customer identifier, e.g. CUST-10001 or CUST-2A7B9."),
]

#: A positive amount of money. ``allow_inf_nan=False`` is not paranoia: Python's
#: ``json.loads`` accepts the non-standard ``NaN`` and ``Infinity`` literals, so
#: they really can arrive over the wire.
Amount = Annotated[
    float,
    Field(
        gt=0,
        le=MAX_REFUND_AMOUNT,
        allow_inf_nan=False,
        description="Refund amount, greater than 0, at most 2 decimal places.",
    ),
]

#: ``min_length`` is declared here so it appears in the published JSON Schema;
#: the whitespace rule below closes the "           " loophole.
Reason = Annotated[
    str,
    StringConstraints(min_length=10, max_length=500),
    Field(description="Why the refund is being issued. At least 10 meaningful characters."),
]


class StrictModel(BaseModel):
    """Base config shared by every tool input."""

    model_config = ConfigDict(
        strict=True,  # no "250.50" -> 250.5 coercion
        extra="forbid",  # unknown arguments are a client bug, not a no-op
        frozen=True,
        validate_default=True,
    )


class GetCustomerRecordInput(StrictModel):
    customer_id: CustomerId


class TriggerRefundInput(StrictModel):
    customer_id: CustomerId
    amount: Amount
    reason: Reason

    @field_validator("amount")
    @classmethod
    def _at_most_two_decimals(cls, value: float) -> float:
        """Money has cents. Reject sub-cent precision instead of rounding it away."""
        if not math.isclose(value * 100, round(value * 100), rel_tol=0, abs_tol=1e-9):
            raise ValueError("amount must have at most 2 decimal places")
        # Normalise to exact cents so downstream arithmetic is clean.
        return round(value, 2)

    @field_validator("reason")
    @classmethod
    def _meaningful_reason(cls, value: str) -> str:
        """``min_length`` counts padding; this counts content."""
        trimmed = value.strip()
        if len(trimmed) < 10:
            raise ValueError("reason must be at least 10 characters after trimming whitespace")
        # Normalise once, at the boundary, so nothing downstream has to remember to.
        return trimmed


def json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema for ``tools/list``, in the shape clients must send."""
    schema = model.model_json_schema(mode="validation")
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return schema


GET_CUSTOMER_RECORD_SCHEMA = json_schema(GetCustomerRecordInput)
TRIGGER_REFUND_SCHEMA = json_schema(TriggerRefundInput)
