"""In-memory stand-in for the billing system.

Swapping this module for real HTTP calls is the only change needed to make the
server production-shaped; the protocol layer in ``server.py`` stays untouched.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal, Union


@dataclass
class CustomerRecord:
    customer_id: str
    name: str
    email: str
    plan: Literal["free", "pro", "enterprise"]
    status: Literal["active", "suspended", "closed"]
    lifetime_value: float
    #: Amount still eligible for refund, in the customer's currency.
    refundable_balance: float
    created_at: str


@dataclass
class Refund:
    refund_id: str
    customer_id: str
    amount: float
    reason: str
    status: str
    created_at: str


_SEED = [
    CustomerRecord(
        "CUST-10001", "Ada Lovelace", "ada@example.com", "enterprise", "active",
        48_200.0, 1_250.0, "2023-02-14T09:31:00+00:00",
    ),
    CustomerRecord(
        "CUST-2A7B9", "Grace Hopper", "grace@example.com", "pro", "active",
        3_150.5, 99.0, "2024-06-01T17:02:00+00:00",
    ),
    CustomerRecord(
        "CUST-30003", "Alan Turing", "alan@example.com", "free", "suspended",
        0.0, 0.0, "2025-01-09T11:45:00+00:00",
    ),
]

_customers: dict[str, CustomerRecord] = {c.customer_id: c for c in _SEED}
_refunds: list[Refund] = []


def get_customer(customer_id: str) -> Union[CustomerRecord, None]:
    return _customers.get(customer_id)


def record_as_dict(record: CustomerRecord) -> dict:
    return asdict(record)


@dataclass
class RefundAccepted:
    refund: Refund
    remaining_balance: float


@dataclass
class RefundRejected:
    """Domain-level (as opposed to schema-level) rejection."""

    kind: Literal["unknown_customer", "account_not_active", "insufficient_balance"]
    detail: dict


def create_refund(customer_id: str, amount: float, reason: str) -> Union[RefundAccepted, RefundRejected]:
    customer = _customers.get(customer_id)
    if customer is None:
        return RefundRejected("unknown_customer", {})

    if customer.status != "active":
        return RefundRejected("account_not_active", {"status": customer.status})

    # Compare in integer cents: 0.1 + 0.2 problems have no place in money.
    cents = round(amount * 100)
    balance_cents = round(customer.refundable_balance * 100)
    if cents > balance_cents:
        return RefundRejected(
            "insufficient_balance", {"refundable_balance": customer.refundable_balance}
        )

    customer.refundable_balance = (balance_cents - cents) / 100

    refund = Refund(
        refund_id=f"RFND-{uuid.uuid4()}",
        customer_id=customer_id,
        amount=cents / 100,
        reason=reason,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    _refunds.append(refund)
    return RefundAccepted(refund, customer.refundable_balance)


def refund_as_dict(refund: Refund) -> dict:
    return asdict(refund)
