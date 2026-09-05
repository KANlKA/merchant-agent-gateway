"""
Batch evaluation harness.

Generates synthetic mandates spanning every accept/reject scenario the
gateway pipeline can produce, submits each through the REAL
process_mandate() pipeline (no shortcuts), and scores actual vs.
expected outcome. Reports a full confusion matrix — false accepts
(the dangerous kind: something that SHOULD have been rejected wasn't)
reported separately from false rejects (safe but annoying: something
fine got blocked).

Run:
    python scripts/batch_eval.py
    python scripts/batch_eval.py --db data/eval.db   # isolated eval DB
"""
from __future__ import annotations

import argparse
import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import security  # noqa: E402
from app.catalog import list_catalog  # noqa: E402
from app.db import use_isolated_test_db  # noqa: E402
from app.gateway import Mandate, process_mandate  # noqa: E402

ACCEPT = "accept"
REJECT = "reject"


def _future(seconds=300) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past(seconds=300) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


class Case:
    __slots__ = ("case_id", "expected", "description", "build")

    def __init__(self, case_id: str, expected: str, description: str, build):
        self.case_id = case_id
        self.expected = expected
        self.description = description
        self.build = build  # callable(agents, catalog) -> Mandate


def _sign(agent, m: Mandate) -> Mandate:
    m.signature = security.sign_mandate(agent.secret, m.payload())
    return m


def generate_cases(agents: dict, catalog: list) -> list[Case]:
    cases: list[Case] = []
    by_sku = {c.sku: c for c in catalog}
    good_agent = agents["good"]
    other_agent = agents["other"]

    # ---- 1) Clean accepts: every catalog item, quantity 1, within policy ----
    for item in catalog:
        qty = 1
        if item.price_paise * qty > 300_000 or qty > item.per_item_limit:
            continue  # would legitimately fail policy; covered separately below
        cases.append(Case(
            f"accept_clean_{item.sku}", ACCEPT, f"Clean single-item order for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_accept_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))

    # ---- 2) Accepts at increasing quantity up to (but not over) limits ----
    for item in catalog:
        max_qty = min(item.per_item_limit, 5)  # merchant-wide cap is 5
        for qty in range(2, max_qty + 1):
            total = item.price_paise * qty
            if total > 300_000:
                break
            cases.append(Case(
                f"accept_qty{qty}_{item.sku}", ACCEPT, f"Order of {qty}x {item.sku}, within all limits",
                lambda a=good_agent, it=item, q=qty: _sign(a, Mandate(
                    mandate_id=f"mandate_accept_qty{q}_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                    quantity=q, claimed_price_paise=it.price_paise,
                    spend_limit_paise=it.price_paise * q, expires_at=_future(), signature="",
                )),
            ))

    # ---- 3) Reject: bad signature ----
    for item in itertools.islice(catalog, 3):
        cases.append(Case(
            f"reject_badsig_{item.sku}", REJECT, f"Corrupted signature for {item.sku}",
            lambda a=good_agent, it=item: _mutate_sig(_sign(a, Mandate(
                mandate_id=f"mandate_badsig_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            ))),
        ))

    # ---- 4) Reject: expired mandate ----
    for item in itertools.islice(catalog, 3):
        cases.append(Case(
            f"reject_expired_{item.sku}", REJECT, f"Expired mandate for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_expired_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_past(), signature="",
            )),
        ))

    # ---- 5) Reject: stale/incorrect claimed price (both directions) ----
    for item in itertools.islice(catalog, 4):
        cases.append(Case(
            f"reject_stale_price_low_{item.sku}", REJECT, f"Claimed price too low for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_staleprice_low_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=max(it.price_paise - 100, 1),
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))
        cases.append(Case(
            f"reject_stale_price_high_{item.sku}", REJECT, f"Claimed price too high for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_staleprice_high_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise + 500,
                spend_limit_paise=it.price_paise + 500, expires_at=_future(), signature="",
            )),
        ))

    # ---- 6) Reject: cart exceeds mandate's own claimed spend limit ----
    for item in itertools.islice(catalog, 4):
        cases.append(Case(
            f"reject_own_spend_limit_{item.sku}", REJECT, f"Cart exceeds mandate's own spend_limit for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_ownlimit_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=max(it.price_paise - 1, 0), expires_at=_future(), signature="",
            )),
        ))

    # ---- 7) Reject: unknown agent (never registered) ----
    for i, item in enumerate(itertools.islice(catalog, 3)):
        cases.append(Case(
            f"reject_unknown_agent_{item.sku}", REJECT, f"Unregistered agent_id for {item.sku}",
            lambda it=item, i=i: Mandate(
                mandate_id=f"mandate_unknownagent_{it.sku}", agent_id=f"agent_ghost_{i}", sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="bogus_sig",
            ),
        ))

    # ---- 8) Reject: forged mandate (agent B signs, claims to be agent A) ----
    for item in itertools.islice(catalog, 3):
        cases.append(Case(
            f"reject_forged_{item.sku}", REJECT, f"Agent B forges a mandate claiming to be agent A, for {item.sku}",
            lambda a=good_agent, forger=other_agent, it=item: _forge(a, forger, it),
        ))

    # ---- 9) Reject: unknown SKU ----
    for i in range(3):
        cases.append(Case(
            f"reject_unknown_sku_{i}", REJECT, "Nonexistent SKU",
            lambda a=good_agent, i=i: _sign(a, Mandate(
                mandate_id=f"mandate_unknownsku_{i}", agent_id=a.agent_id, sku=f"SKU-GHOST-{i}",
                quantity=1, claimed_price_paise=1000, spend_limit_paise=1000,
                expires_at=_future(), signature="",
            )),
        ))

    # ---- 10) Reject: merchant policy — over max order value ----
    for item in [c for c in catalog if c.price_paise > 300_000][:3]:
        cases.append(Case(
            f"reject_policy_maxorder_{item.sku}", REJECT, f"Over merchant max_order_value for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_maxorder_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))

    # ---- 11) Reject: merchant policy — over per-item limit ----
    for item in catalog:
        qty = item.per_item_limit + 1
        if qty > 5 or item.price_paise * qty > 5_000_000:
            continue
        cases.append(Case(
            f"reject_policy_peritem_{item.sku}", REJECT, f"Over per-item limit for {item.sku}",
            lambda a=good_agent, it=item, q=qty: _sign(a, Mandate(
                mandate_id=f"mandate_peritem_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=q, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise * q, expires_at=_future(), signature="",
            )),
        ))

    # ---- 12) Reject: merchant policy — over merchant-wide max quantity ----
    for item in itertools.islice((c for c in catalog if c.per_item_limit >= 6), 4):
        qty = 6
        cases.append(Case(
            f"reject_policy_maxqty_{item.sku}", REJECT, f"Over merchant-wide max_quantity_per_mandate for {item.sku}",
            lambda a=good_agent, it=item, q=qty: _sign(a, Mandate(
                mandate_id=f"mandate_maxqty_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=q, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise * q, expires_at=_future(), signature="",
            )),
        ))

    # ---- 13) Reject: duplicate mandate_id (replay) ----
    for item in itertools.islice(catalog, 3):
        shared_id = f"mandate_replay_{item.sku}"
        # first submission (accepted) is issued directly in run_evaluation();
        # here we add the SECOND, duplicate submission as the scored case.
        cases.append(Case(
            f"reject_replay_{item.sku}", REJECT, f"Replayed/duplicate mandate_id for {item.sku}",
            lambda a=good_agent, it=item, mid=shared_id: _sign(a, Mandate(
                mandate_id=mid, agent_id=a.agent_id, sku=it.sku,
                quantity=1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))

    # ---- 14) Reject: zero or negative quantity ----
    for item in itertools.islice(catalog, 6):
        cases.append(Case(
            f"reject_zero_qty_{item.sku}", REJECT, f"Zero quantity for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_zeroqty_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=0, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))
        cases.append(Case(
            f"reject_negative_qty_{item.sku}", REJECT, f"Negative quantity for {item.sku}",
            lambda a=good_agent, it=item: _sign(a, Mandate(
                mandate_id=f"mandate_negqty_{it.sku}", agent_id=a.agent_id, sku=it.sku,
                quantity=-1, claimed_price_paise=it.price_paise,
                spend_limit_paise=it.price_paise, expires_at=_future(), signature="",
            )),
        ))

    # ---- 15) Reject: mandate signed by an agent that isn't the one it names,
    #          using a SECOND independent forger identity per item (covers
    #          more of the credential-isolation surface than case 8 alone) ----
    third_agent = agents.get("third")
    if third_agent is not None:
        for item in itertools.islice(catalog, 3):
            cases.append(Case(
                f"reject_forged_v2_{item.sku}", REJECT, f"Second forger identity impersonates good agent for {item.sku}",
                lambda a=good_agent, forger=third_agent, it=item: _forge(
                    a, forger, it, mandate_id=f"mandate_forged_v2_{it.sku}"
                ),
            ))

    return cases


def _mutate_sig(m: Mandate) -> Mandate:
    m.signature = ("0" if m.signature[0] != "0" else "1") + m.signature[1:]
    return m


def _forge(victim_agent, forger_agent, item, mandate_id=None) -> Mandate:
    m = Mandate(
        mandate_id=mandate_id or f"mandate_forged_{item.sku}",
        agent_id=victim_agent.agent_id,  # claims to be the victim
        sku=item.sku, quantity=1, claimed_price_paise=item.price_paise,
        spend_limit_paise=item.price_paise, expires_at=_future(), signature="",
    )
    m.signature = security.sign_mandate(forger_agent.secret, m.payload())  # signed with the WRONG secret
    return m


def run_evaluation(db_path: Path) -> dict:
    use_isolated_test_db(db_path)

    agents = {
        "good": security.register_agent("eval-good-agent"),
        "other": security.register_agent("eval-other-agent"),
        "third": security.register_agent("eval-third-agent"),
    }
    catalog = list_catalog()
    cases = generate_cases(agents, catalog)

    # Pre-submit the first half of each replay pair so the "duplicate"
    # scored case has something real to collide with.
    for item in itertools.islice(catalog, 3):
        first = Mandate(
            mandate_id=f"mandate_replay_{item.sku}", agent_id=agents["good"].agent_id, sku=item.sku,
            quantity=1, claimed_price_paise=item.price_paise,
            spend_limit_paise=item.price_paise, expires_at=_future(), signature="",
        )
        first.signature = security.sign_mandate(agents["good"].secret, first.payload())
        process_mandate(first)

    rows = []
    confusion = {"true_accept": 0, "true_reject": 0, "false_accept": 0, "false_reject": 0}

    for case in cases:
        mandate = case.build()
        result = process_mandate(mandate)
        actual = ACCEPT if result.accepted else REJECT

        if case.expected == ACCEPT and actual == ACCEPT:
            confusion["true_accept"] += 1
        elif case.expected == REJECT and actual == REJECT:
            confusion["true_reject"] += 1
        elif case.expected == REJECT and actual == ACCEPT:
            confusion["false_accept"] += 1  # dangerous: should have been blocked
        else:
            confusion["false_reject"] += 1  # safe but wrong: blocked something fine

        rows.append({
            "case_id": case.case_id,
            "description": case.description,
            "expected": case.expected,
            "actual": actual,
            "correct": case.expected == actual,
            "verification_result": result.verification_result,
            "policy_result": result.policy_result,
            "reason": result.verification_reason if result.verification_result == "fail"
                      else result.policy_reason,
        })

    return {"rows": rows, "confusion": confusion, "n_cases": len(cases)}


def print_report(report: dict) -> None:
    rows, confusion, n = report["rows"], report["confusion"], report["n_cases"]
    correct = sum(1 for r in rows if r["correct"])

    print(f"\nBatch evaluation: {n} cases\n" + "=" * 60)
    for r in rows:
        mark = "✓" if r["correct"] else "✗ MISMATCH"
        print(f"[{mark}] {r['case_id']:<32} expected={r['expected']:<7} actual={r['actual']:<7} — {r['reason']}")

    print("\n" + "=" * 60)
    print("Confusion matrix")
    print(f"  True accept   (correctly accepted): {confusion['true_accept']}")
    print(f"  True reject   (correctly rejected):  {confusion['true_reject']}")
    print(f"  False accept  (SHOULD have been rejected — DANGEROUS): {confusion['false_accept']}")
    print(f"  False reject  (safe item wrongly blocked):              {confusion['false_reject']}")
    print(f"\nAccuracy: {correct}/{n} = {correct / n:.1%}")
    if confusion["false_accept"] > 0:
        print("\n⚠️  FALSE ACCEPTS DETECTED — review immediately, this is a security-relevant failure.")
    else:
        print("\nNo false accepts. All rejection scenarios were correctly blocked.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/eval.db", help="Isolated DB file for this eval run")
    args = parser.parse_args()

    db_path = Path(args.db)
    if db_path.exists():
        db_path.unlink()

    report = run_evaluation(db_path)
    print_report(report)

    n = report["n_cases"]
    correct = sum(1 for r in report["rows"] if r["correct"])
    sys.exit(0 if correct == n else 1)
