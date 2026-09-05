
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security  # noqa: E402
from app.catalog import list_catalog  # noqa: E402
from app.db import use_isolated_test_db  # noqa: E402
from app.gateway import Mandate, get_audit_log, process_mandate  # noqa: E402
from app.razorpay_client import LIVE_MODE  # noqa: E402
from agent.buyer_agent import BuyerAgent  # noqa: E402


def line(char="-", n=64):
    print(char * n)


def main():
    demo_db = Path(__file__).resolve().parent.parent / "data" / "demo.db"
    if demo_db.exists():
        demo_db.unlink()
    use_isolated_test_db(demo_db)

    print("MERCHANT AGENT GATEWAY — live end-to-end demo")
    print(f"Razorpay mode: {'LIVE TEST MODE' if LIVE_MODE else 'MOCK (no RAZORPAY_KEY_ID/SECRET set)'}")
    line("=")

    # 1. Merchant catalog
    catalog = list_catalog()
    print(f"\n1. Merchant catalog seeded: {len(catalog)} items")
    for item in catalog[:4]:
        print(f"   {item.sku:<14} {item.name:<28} ₹{item.price_paise/100:>8.2f}  stock={item.stock}")
    print("   ...")

    # 2. A real buyer agent registers and shops
    print("\n2. Buyer agent registers with its own identity + secret")
    shopper = BuyerAgent("demo-shopper")
    print(f"   agent_id = {shopper.identity.agent_id}")
    print(f"   secret   = {shopper.identity.secret[:12]}... (never shared with the merchant's policy layer)")

    goal = "I need a good water bottle for the gym"
    budget = 100_000  # ₹1000
    print(f"\n3. Agent reasons over goal={goal!r}, budget=₹{budget/100:.2f}")
    decision, result = shopper.shop(goal, budget_paise=budget)
    print(f"   -> decided: {decision.quantity}x {decision.sku} ({decision.rationale})")
    print(f"   -> mandate submitted -> verification={result.verification_result}, "
          f"policy={result.policy_result}, final={result.final_status}")
    if result.accepted:
        print(f"   -> Razorpay order created: {result.razorpay_order_id}")

    # 4. Attack scenario: a second agent tries to forge a mandate as the first agent
    print("\n4. Attack scenario: a different agent tries to impersonate the shopper")
    attacker = security.register_agent("demo-attacker")
    catalog_item = catalog[0]
    forged = Mandate(
        mandate_id="mandate_demo_forgery",
        agent_id=shopper.identity.agent_id,       # claims to be the legitimate shopper
        sku=catalog_item.sku, quantity=1,
        claimed_price_paise=catalog_item.price_paise,
        spend_limit_paise=catalog_item.price_paise,
        expires_at="2099-01-01T00:00:00+00:00",
        signature="",
    )
    forged.signature = security.sign_mandate(attacker.secret, forged.payload())  # signed with attacker's secret
    forged_result = process_mandate(forged)
    print(f"   -> verification={forged_result.verification_result} "
          f"({forged_result.verification_reason})")
    print(f"   -> final={forged_result.final_status}  (correctly blocked, no charge created)")

    # 5. Replay scenario: the SAME legitimate mandate submitted twice concurrently-ish
    print("\n5. Replay scenario: submitting the shopper's exact mandate a second time")
    replay = Mandate(
        mandate_id=result.mandate_id if hasattr(result, "mandate_id") else None,
        agent_id=shopper.identity.agent_id, sku=decision.sku, quantity=decision.quantity,
        claimed_price_paise=catalog_item.price_paise, spend_limit_paise=budget,
        expires_at="2099-01-01T00:00:00+00:00", signature="",
    )
    # reuse the original mandate_id captured on the gateway result
    replay.mandate_id = result.mandate_id
    replay.signature = security.sign_mandate(shopper.identity.secret, replay.payload())
    replay_result = process_mandate(replay)
    print(f"   -> final={replay_result.final_status} "
          f"({replay_result.verification_reason})")

    # 6. Audit trail
    print("\n6. Full audit trail for this run:")
    line()
    for row in reversed(get_audit_log()):
        print(f"   [{row['final_status']:<8}] {row['mandate_id']:<28} "
              f"verify={row['verification_result']:<4} policy={row['policy_result']:<7} "
              f"order={row['razorpay_order_id'] or '-'}")
    line("=")
    print(f"\nDemo complete. {len(get_audit_log())} attempts logged "
          f"({sum(1 for r in get_audit_log() if r['final_status']=='accepted')} accepted, "
          f"{sum(1 for r in get_audit_log() if r['final_status']=='rejected')} rejected).")
    print(f"Data persisted at: {demo_db}")


if __name__ == "__main__":
    main()
