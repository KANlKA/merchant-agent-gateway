"""
The merchant policy gate.

This is deliberately a SEPARATE check from mandate verification. A
mandate can be perfectly signed, unexpired, and priced correctly
against the live catalog (verification: pass) and still be rejected
here — because the merchant's own risk rules say no. The buyer agent
has no visibility into these rules and cannot satisfy this gate by
claiming anything about itself; it only sees pass/fail + reason.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.catalog import CatalogItem

# Merchant-level policy. In production this would live in a merchant
# settings table; hardcoded here per the single-merchant scope of v1.
MERCHANT_POLICY = {
    "max_order_value_paise": 300_000,     # ₹3,000 per order, merchant-wide
    "allowed_categories": {
        "electronics", "stationery", "home", "apparel", "accessories", "gift-cards",
    },
    "blocked_categories": set(),          # e.g. could block "gift-cards" for risk
    "max_quantity_per_mandate": 5,
}


@dataclass
class PolicyResult:
    passed: bool
    reason: str


def evaluate_policy(item: CatalogItem, quantity: int, cart_total_paise: int) -> PolicyResult:
    if item.category in MERCHANT_POLICY["blocked_categories"]:
        return PolicyResult(False, f"category '{item.category}' is blocked by merchant policy")

    if item.category not in MERCHANT_POLICY["allowed_categories"]:
        return PolicyResult(False, f"category '{item.category}' is not in allowed categories")

    if quantity > MERCHANT_POLICY["max_quantity_per_mandate"]:
        return PolicyResult(
            False,
            f"quantity {quantity} exceeds merchant max_quantity_per_mandate "
            f"({MERCHANT_POLICY['max_quantity_per_mandate']})",
        )

    if quantity > item.per_item_limit:
        return PolicyResult(
            False, f"quantity {quantity} exceeds per-item limit ({item.per_item_limit}) for {item.sku}"
        )

    if cart_total_paise > MERCHANT_POLICY["max_order_value_paise"]:
        return PolicyResult(
            False,
            f"cart total {cart_total_paise} paise exceeds merchant "
            f"max_order_value_paise ({MERCHANT_POLICY['max_order_value_paise']})",
        )

    if item.stock < quantity:
        return PolicyResult(False, f"insufficient stock for {item.sku}: have {item.stock}, need {quantity}")

    return PolicyResult(True, "within merchant policy")
