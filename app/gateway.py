"""
Core mandate-processing pipeline. Deliberately framework-agnostic
(plain functions, stdlib + app.* only) so it can be:
  - called directly (scripts/batch_eval.py, scripts/run_demo.py, tests)
  - wrapped by FastAPI (app/main.py) for real HTTP

Pipeline, in order:
  1. reserve_mandate()   -- ATOMIC. Claims the mandate_id via a DB
                             UNIQUE constraint before anything else
                             happens. This is what closes the race
                             condition: two near-simultaneous submits
                             of the same mandate race to INSERT, and
                             SQLite guarantees only one wins.
  2. verify_mandate()    -- signature valid? unexpired? priced correctly
                             against the LIVE catalog (catches stale
                             carts)? within the mandate's own claimed
                             spend limit?
  3. evaluate_policy()   -- SEPARATE gate: does merchant policy allow
                             this at all? Passing (2) does not satisfy
                             (3) and vice versa. Includes a first-pass,
                             non-atomic stock check for a fast, clear
                             rejection reason in the common case.
  4. reserve_stock()     -- ATOMIC. The real stock guarantee: a single
                             UPDATE ... WHERE stock >= quantity, so two
                             concurrent mandates for the last unit of
                             an item cannot both succeed even though
                             both may have passed step 3's (necessarily
                             stale-by-the-time-it-runs) read-only check.
                             Mirrors reserve_mandate()'s fix exactly,
                             for inventory instead of mandate identity.
  5. create_order()      -- only on triple-pass. Razorpay order (real
                             test-mode or mock, see razorpay_client.py).
                             If this fails, reserved stock is released
                             back rather than permanently lost.
  6. audit log           -- every attempt, pass or fail, gets one row.
                             Never a silent failure and never a charge
                             without a logged reason.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app import security
from app.catalog import get_item, reserve_stock, release_stock
from app.db import cursor
from app.policy import evaluate_policy
from app.razorpay_client import create_order


@dataclass
class Mandate:
    mandate_id: str
    agent_id: str
    sku: str
    quantity: int
    claimed_price_paise: int   # what the agent believes the unit price is
    spend_limit_paise: int     # the agent's own self-claimed ceiling
    expires_at: str            # ISO8601
    signature: str

    def payload(self) -> dict[str, Any]:
        """The exact fields that were signed. Order doesn't matter —
        security.sign_mandate() canonicalizes — but the SET of fields
        must match exactly what the agent signed, or verification
        correctly fails."""
        return {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "sku": self.sku,
            "quantity": self.quantity,
            "claimed_price_paise": self.claimed_price_paise,
            "spend_limit_paise": self.spend_limit_paise,
            "expires_at": self.expires_at,
        }


@dataclass
class GatewayResult:
    mandate_id: str
    accepted: bool
    verification_result: str          # "pass" | "fail"
    verification_reason: str
    policy_result: str                # "pass" | "fail" | "skipped"
    policy_reason: str
    razorpay_order_id: Optional[str] = None
    razorpay_mode: Optional[str] = None   # "live_test_mode" | "mock" | None if no order was created
    final_status: str = "rejected"    # "accepted" | "rejected"


class MandateAlreadyClaimed(Exception):
    """Raised when reserve_mandate loses the atomic-insert race."""


def reserve_mandate(m: Mandate) -> None:
    """Atomically claim mandate_id. This MUST happen before verification
    or policy evaluation — reserving first, checking second — so that
    two concurrent submissions of the identical mandate can never both
    reach order creation. The loser gets MandateAlreadyClaimed."""
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO mandates "
                "(mandate_id, agent_id, sku, quantity, claimed_price_paise, "
                " spend_limit_paise, expires_at, signature) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    m.mandate_id, m.agent_id, m.sku, m.quantity,
                    m.claimed_price_paise, m.spend_limit_paise,
                    m.expires_at, m.signature,
                ),
            )
    except sqlite3.IntegrityError as e:
        raise MandateAlreadyClaimed(str(e)) from e


def verify_mandate(m: Mandate) -> tuple[bool, str]:
    """Independent layer #1: is this mandate itself legitimate?"""
    agent = security.get_agent(m.agent_id)
    if agent is None:
        return False, f"unknown agent_id '{m.agent_id}'"

    if not security.verify_signature(agent.secret, m.payload(), m.signature):
        return False, "signature verification failed"

    try:
        expires_dt = datetime.fromisoformat(m.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False, f"unparseable expires_at '{m.expires_at}'"
    if expires_dt < datetime.now(timezone.utc):
        return False, f"mandate expired at {m.expires_at}"

    item = get_item(m.sku)
    if item is None:
        return False, f"unknown sku '{m.sku}'"

    if item.price_paise != m.claimed_price_paise:
        return False, (
            f"stale price: mandate claims {m.claimed_price_paise} paise, "
            f"live catalog price is {item.price_paise} paise"
        )

    cart_total = item.price_paise * m.quantity
    if cart_total > m.spend_limit_paise:
        return False, (
            f"cart total {cart_total} paise exceeds the mandate's own "
            f"claimed spend_limit_paise ({m.spend_limit_paise})"
        )

    if m.quantity <= 0:
        return False, f"invalid quantity {m.quantity}"

    return True, "signature, expiry, live price, and self-claimed spend limit all check out"


def process_mandate(m: Mandate) -> GatewayResult:
    """Run the full pipeline for one mandate. Always writes exactly one
    audit_log row, whatever the outcome."""

    # Step 1: atomic reserve, before any other logic runs.
    try:
        reserve_mandate(m)
    except MandateAlreadyClaimed:
        result = GatewayResult(
            mandate_id=m.mandate_id,
            accepted=False,
            verification_result="fail",
            verification_reason="mandate_id already claimed (duplicate/replay submission)",
            policy_result="skipped",
            policy_reason="not evaluated — mandate already claimed",
        )
        _write_audit(m, result, cart_total_paise=None)
        return result

    # Step 2: verify.
    ok, reason = verify_mandate(m)
    if not ok:
        result = GatewayResult(
            mandate_id=m.mandate_id,
            accepted=False,
            verification_result="fail",
            verification_reason=reason,
            policy_result="skipped",
            policy_reason="not evaluated — verification failed first",
        )
        _write_audit(m, result, cart_total_paise=None)
        return result

    item = get_item(m.sku)
    cart_total = item.price_paise * m.quantity

    # Step 3: independent merchant policy gate.
    policy = evaluate_policy(item, m.quantity, cart_total)
    if not policy.passed:
        result = GatewayResult(
            mandate_id=m.mandate_id,
            accepted=False,
            verification_result="pass",
            verification_reason=reason,
            policy_result="fail",
            policy_reason=policy.reason,
        )
        _write_audit(m, result, cart_total_paise=cart_total)
        return result

    # Step 4: atomic stock reservation. evaluate_policy()'s stock check
    # above was a plain read and can be stale under concurrency -- two
    # mandates for the very last unit could both pass it. This is the
    # real atomic gate, structured exactly like reserve_mandate(): a
    # single UPDATE with a WHERE clause, so only one concurrent caller
    # can ever actually decrement into a valid (non-negative) remainder.
    if not reserve_stock(item.sku, m.quantity):
        result = GatewayResult(
            mandate_id=m.mandate_id,
            accepted=False,
            verification_result="pass",
            verification_reason=reason,
            policy_result="fail",
            policy_reason=(
                f"insufficient stock for {item.sku} at reservation time "
                f"(a concurrent order consumed it first -- the earlier "
                f"policy check read a stale stock count)"
            ),
        )
        _write_audit(m, result, cart_total_paise=cart_total)
        return result

    # Step 5: both gates passed and stock is reserved -> create the order.
    try:
        order = create_order(
            amount_paise=cart_total,
            receipt=m.mandate_id,
            notes={"agent_id": m.agent_id, "sku": m.sku, "quantity": str(m.quantity)},
        )
    except Exception as e:
        release_stock(item.sku, m.quantity)  # order failed -- give the stock back
        result = GatewayResult(
            mandate_id=m.mandate_id,
            accepted=False,
            verification_result="pass",
            verification_reason=reason,
            policy_result="pass",
            policy_reason=policy.reason,
            razorpay_order_id=None,
            final_status="rejected",
        )
        result.policy_reason += f" | order creation failed: {e}"
        _write_audit(m, result, cart_total_paise=cart_total)
        return result

    result = GatewayResult(
        mandate_id=m.mandate_id,
        accepted=True,
        verification_result="pass",
        verification_reason=reason,
        policy_result="pass",
        policy_reason=policy.reason,
        razorpay_order_id=order.order_id,
        razorpay_mode=order.mode,
        final_status="accepted",
    )
    _write_audit(m, result, cart_total_paise=cart_total)
    return result


def _write_audit(m: Mandate, result: GatewayResult, cart_total_paise: Optional[int]) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log "
            "(mandate_id, agent_id, sku, cart_total_paise, verification_result, "
            " verification_reason, policy_result, policy_reason, razorpay_order_id, "
            " razorpay_mode, final_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                m.mandate_id, m.agent_id, m.sku, cart_total_paise,
                result.verification_result, result.verification_reason,
                result.policy_result, result.policy_reason,
                result.razorpay_order_id, result.razorpay_mode, result.final_status,
            ),
        )


def get_audit_log(limit: int = 200) -> list[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cur.fetchall()]
