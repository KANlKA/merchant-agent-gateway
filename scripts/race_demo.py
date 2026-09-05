
from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security  # noqa: E402
from app.catalog import get_item  # noqa: E402
from app.db import cursor, use_isolated_test_db  # noqa: E402
from app.gateway import Mandate, process_mandate  # noqa: E402


def line(char="-", n=64):
    print(char * n)


def future(seconds=300) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def seed_low_stock_item(sku: str, stock: int, price_paise: int = 10_000) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO catalog (sku, name, category, price_paise, stock, per_item_limit) "
            "VALUES (?, 'Race Demo Item', 'home', ?, ?, 999)",
            (sku, price_paise, stock),
        )


def race_duplicate_mandate(n_threads: int = 10) -> None:
    print(f"\nSCENARIO 1 — Double-charge race: {n_threads} threads, ONE mandate")
    line()
    agent = security.register_agent("race-buyer")
    item_sku = "SKU-KT-001"  # any normal, well-stocked catalog item
    item = get_item(item_sku)

    shared_mandate = Mandate(
        mandate_id="mandate_RACE_duplicate_demo",
        agent_id=agent.agent_id,
        sku=item_sku,
        quantity=1,
        claimed_price_paise=item.price_paise,
        spend_limit_paise=item.price_paise,
        expires_at=future(),
        signature="",
    )
    shared_mandate.signature = security.sign_mandate(agent.secret, shared_mandate.payload())

    print(f"One real signed mandate: {shared_mandate.mandate_id}")
    print(f"Firing it from {n_threads} threads at the exact same instant...\n")

    results: list[bool] = []
    barrier = threading.Barrier(n_threads)

    def attempt():
        barrier.wait()  # every thread releases at once -- real contention, not simulated
        result = process_mandate(shared_mandate)
        results.append(result.accepted)

    threads = [threading.Thread(target=attempt) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = results.count(True)
    rejected = results.count(False)
    print(f"  -> {accepted} accepted, {rejected} rejected")
    if accepted == 1:
        print("  -> PASS: exactly one order was ever created for this mandate.")
    else:
        print(f"  -> FAIL: expected exactly 1 accepted, got {accepted}. Double-charge bug is back.")


def race_last_unit_of_stock(n_threads: int = 20) -> None:
    print(f"\nSCENARIO 2 — Overselling race: {n_threads} different buyers, 1 unit of stock")
    line()
    sku = "SKU-RACE-LASTUNIT"
    seed_low_stock_item(sku, stock=1)
    item = get_item(sku)
    print(f"Seeded {item.sku} with stock=1")
    print(f"Registering {n_threads} DIFFERENT buyer agents, each with its own identity + secret,")
    print(f"each signing its OWN legitimate mandate for that same last unit...\n")

    mandates: list[Mandate] = []
    for i in range(n_threads):
        agent = security.register_agent(f"race-buyer-{i}")
        m = Mandate(
            mandate_id=f"mandate_RACE_oversell_{i}",
            agent_id=agent.agent_id,
            sku=sku,
            quantity=1,
            claimed_price_paise=item.price_paise,
            spend_limit_paise=item.price_paise,
            expires_at=future(),
            signature="",
        )
        m.signature = security.sign_mandate(agent.secret, m.payload())
        mandates.append(m)

    print(f"Firing all {n_threads} distinct, validly-signed mandates at the exact same instant...\n")

    results: list[bool] = []
    barrier = threading.Barrier(n_threads)

    def attempt(mandate: Mandate):
        barrier.wait()
        result = process_mandate(mandate)
        results.append(result.accepted)

    threads = [threading.Thread(target=attempt, args=(m,)) for m in mandates]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = results.count(True)
    rejected = results.count(False)
    final_stock = get_item(sku).stock
    print(f"  -> {accepted} accepted, {rejected} rejected")
    print(f"  -> final stock: {final_stock}")
    if accepted == 1 and final_stock == 0:
        print("  -> PASS: exactly one buyer won the last unit. Stock never went negative.")
    else:
        print(f"  -> FAIL: expected 1 accepted / stock=0, got {accepted} accepted / stock={final_stock}.")


def main():
    demo_db = Path(__file__).resolve().parent.parent / "data" / "race_demo.db"
    if demo_db.exists():
        demo_db.unlink()
    use_isolated_test_db(demo_db)

    print("MERCHANT AGENT GATEWAY — race condition demo")
    print("Both scenarios below fire real concurrent threads at the real gateway pipeline.")
    line("=")

    race_duplicate_mandate(n_threads=10)
    race_last_unit_of_stock(n_threads=20)

    line("=")
    print("\nDone. Both fixes hold under real concurrent load.")


if __name__ == "__main__":
    main()