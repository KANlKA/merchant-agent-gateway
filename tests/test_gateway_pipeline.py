import threading
import time
from datetime import datetime, timedelta, timezone

from app import security
from app.catalog import get_item
from app.gateway import Mandate, process_mandate, reserve_mandate, MandateAlreadyClaimed
from tests.base import GatewayTestCase


def _future(seconds=300) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past(seconds=300) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_signed_mandate(agent, sku="SKU-KT-001", quantity=1, mandate_id=None,
                          price_override=None, spend_limit_override=None, expires_at=None):
    item = get_item(sku)
    price = price_override if price_override is not None else item.price_paise
    spend_limit = spend_limit_override if spend_limit_override is not None else price * quantity
    m = Mandate(
        mandate_id=mandate_id or f"mandate_{threading.get_ident()}_{time.time_ns()}",
        agent_id=agent.agent_id,
        sku=sku,
        quantity=quantity,
        claimed_price_paise=price,
        spend_limit_paise=spend_limit,
        expires_at=expires_at or _future(),
        signature="",
    )
    m.signature = security.sign_mandate(agent.secret, m.payload())
    return m


class TestHappyPath(GatewayTestCase):
    def test_valid_mandate_is_accepted_and_creates_order(self):
        agent = security.register_agent("test-agent")
        mandate = _make_signed_mandate(agent)
        result = process_mandate(mandate)
        self.assertTrue(result.accepted)
        self.assertEqual(result.final_status, "accepted")
        self.assertEqual(result.verification_result, "pass")
        self.assertEqual(result.policy_result, "pass")
        self.assertIsNotNone(result.razorpay_order_id)
        self.assertTrue(result.razorpay_order_id.startswith("order_"))

    def test_accepted_mandate_is_logged_to_audit_trail(self):
        from app.gateway import get_audit_log
        agent = security.register_agent("test-agent")
        mandate = _make_signed_mandate(agent)
        process_mandate(mandate)
        log = get_audit_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["final_status"], "accepted")
        self.assertEqual(log[0]["mandate_id"], mandate.mandate_id)

    def test_audit_log_records_which_razorpay_mode_created_the_order(self):
        """Regression test for a doc/code mismatch found during review:
        razorpay_client.py's docstring claimed 'the audit log always
        records which mode created the order', but audit_log had no
        such column and the value was silently discarded. Now fixed
        end to end -- schema, gateway, and API response all carry it."""
        from app.gateway import get_audit_log
        agent = security.register_agent("mode-test-agent")
        mandate = _make_signed_mandate(agent)
        result = process_mandate(mandate)

        self.assertEqual(result.razorpay_mode, "mock")  # no real keys set in test env
        log = get_audit_log()
        self.assertEqual(log[0]["razorpay_mode"], "mock")


class TestVerificationFailures(GatewayTestCase):
    def test_unknown_agent_is_rejected(self):
        item = get_item("SKU-KT-001")
        m = Mandate(
            mandate_id="mandate_unknown_agent",
            agent_id="agent_ghost",
            sku="SKU-KT-001",
            quantity=1,
            claimed_price_paise=item.price_paise,
            spend_limit_paise=item.price_paise,
            expires_at=_future(),
            signature="deadbeef",
        )
        result = process_mandate(m)
        self.assertFalse(result.accepted)
        self.assertIn("unknown agent_id", result.verification_reason)

    def test_bad_signature_is_rejected(self):
        agent = security.register_agent("test-agent")
        mandate = _make_signed_mandate(agent)
        mandate.signature = "0" * 64  # corrupt it
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertIn("signature", result.verification_reason)

    def test_expired_mandate_is_rejected(self):
        agent = security.register_agent("test-agent")
        mandate = _make_signed_mandate(agent, expires_at=_past())
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertIn("expired", result.verification_reason)

    def test_stale_price_is_rejected(self):
        """The agent signed a mandate at an old/incorrect price — the
        live catalog price no longer matches. Must be caught, not
        silently honored at the stale price."""
        agent = security.register_agent("test-agent")
        item = get_item("SKU-KT-001")
        mandate = _make_signed_mandate(agent, price_override=item.price_paise - 100)
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertIn("stale price", result.verification_reason)

    def test_cart_exceeding_own_claimed_spend_limit_is_rejected(self):
        """Even the mandate's OWN self-declared spend limit must hold —
        catches a malformed/inconsistent mandate before it ever reaches
        merchant policy."""
        agent = security.register_agent("test-agent")
        item = get_item("SKU-KT-001")
        mandate = _make_signed_mandate(
            agent, spend_limit_override=item.price_paise - 1
        )
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertIn("spend_limit", result.verification_reason)

    def test_unknown_sku_is_rejected(self):
        agent = security.register_agent("test-agent")
        m = Mandate(
            mandate_id="mandate_bad_sku",
            agent_id=agent.agent_id,
            sku="SKU-NOPE",
            quantity=1,
            claimed_price_paise=100,
            spend_limit_paise=100,
            expires_at=_future(),
            signature="",
        )
        m.signature = security.sign_mandate(agent.secret, m.payload())
        result = process_mandate(m)
        self.assertFalse(result.accepted)
        self.assertIn("unknown sku", result.verification_reason)


class TestPolicyGateIndependence(GatewayTestCase):
    def test_verification_pass_but_policy_fail_is_rejected(self):
        """A perfectly legitimate, correctly-signed, correctly-priced
        mandate can still be rejected by merchant policy — the two
        gates are independent."""
        agent = security.register_agent("test-agent")
        item = get_item("SKU-GC-001")  # ₹5000 gift card, over merchant's ₹3000 cap
        mandate = _make_signed_mandate(agent, sku="SKU-GC-001", spend_limit_override=item.price_paise)
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertEqual(result.verification_result, "pass")
        self.assertEqual(result.policy_result, "fail")

    def test_agent_cannot_satisfy_policy_by_self_declaring_a_high_spend_limit(self):
        """Central trust boundary: an agent claiming a huge spend_limit
        does not make the merchant's own policy cap disappear."""
        agent = security.register_agent("test-agent")
        item = get_item("SKU-GC-001")
        mandate = _make_signed_mandate(
            agent, sku="SKU-GC-001", spend_limit_override=100_000_000  # agent claims a huge ceiling
        )
        result = process_mandate(mandate)
        self.assertFalse(result.accepted)
        self.assertEqual(result.policy_result, "fail")


class TestStockRollbackOnOrderFailure(GatewayTestCase):
    def test_stock_is_released_if_order_creation_fails(self):
        """If Razorpay order creation raises AFTER stock was already
        atomically reserved, that stock must be given back -- otherwise
        a failed payment would permanently strand inventory the
        merchant never actually sold."""
        import app.gateway as gateway_module

        agent = security.register_agent("rollback-test-agent")
        mandate = _make_signed_mandate(agent)
        item_before = get_item(mandate.sku)
        stock_before = item_before.stock

        def failing_create_order(*args, **kwargs):
            raise RuntimeError("simulated Razorpay outage")

        original = gateway_module.create_order
        gateway_module.create_order = failing_create_order
        try:
            result = process_mandate(mandate)
        finally:
            gateway_module.create_order = original

        self.assertFalse(result.accepted)
        self.assertIn("order creation failed", result.policy_reason)
        self.assertEqual(
            get_item(mandate.sku).stock, stock_before,
            "stock must be restored to its pre-reservation level after a failed order",
        )


class TestAtomicReserveConcurrency(GatewayTestCase):
    """Regression test for the real race condition found via
    deliberate stress-testing: two near-simultaneous submissions of
    the IDENTICAL mandate_id must not both succeed. Before the fix,
    a naive 'check if seen, then act' approach let both requests read
    an empty 'not seen yet' state and both proceed to order creation —
    a real double-charge bug. reserve_mandate()'s DB-level UNIQUE
    constraint closes it."""

    def test_duplicate_mandate_id_only_reserved_once_under_concurrency(self):
        agent = security.register_agent("race-agent")
        shared_mandate_id = "mandate_race_condition_test"
        mandate = _make_signed_mandate(agent, mandate_id=shared_mandate_id)

        results = []
        errors = []
        barrier = threading.Barrier(10)

        def attempt():
            try:
                barrier.wait()  # maximize actual concurrent contention
                reserve_mandate(mandate)
                results.append("reserved")
            except MandateAlreadyClaimed:
                results.append("rejected")
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        self.assertEqual(results.count("reserved"), 1, "exactly one submission must win the reserve race")
        self.assertEqual(results.count("rejected"), 9)

    def test_duplicate_mandate_processed_end_to_end_only_charges_once(self):
        """Same scenario but through the full process_mandate() pipeline,
        proving only one Razorpay order is ever created for a duplicate
        submission — the property that actually matters for a merchant."""
        agent = security.register_agent("race-agent-2")
        shared_mandate_id = "mandate_race_e2e"
        mandate = _make_signed_mandate(agent, mandate_id=shared_mandate_id)

        outcomes = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            outcomes.append(process_mandate(mandate))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        accepted = [o for o in outcomes if o.accepted]
        self.assertEqual(len(accepted), 1, "exactly one of the duplicate submissions may be accepted")
        order_ids = {o.razorpay_order_id for o in accepted}
        self.assertEqual(len(order_ids), 1)

    def test_two_distinct_mandates_both_succeed_independently(self):
        """Sanity check: the atomic reserve only blocks TRUE duplicates,
        not unrelated concurrent traffic."""
        agent = security.register_agent("distinct-agent")
        m1 = _make_signed_mandate(agent, mandate_id="mandate_distinct_1")
        m2 = _make_signed_mandate(agent, mandate_id="mandate_distinct_2")

        results = {}

        def run(m, key):
            results[key] = process_mandate(m)

        t1 = threading.Thread(target=run, args=(m1, "a"))
        t2 = threading.Thread(target=run, args=(m2, "b"))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertTrue(results["a"].accepted)
        self.assertTrue(results["b"].accepted)
        self.assertNotEqual(results["a"].razorpay_order_id, results["b"].razorpay_order_id)
