import threading

from app.catalog import CatalogItem, get_item, list_catalog, reserve_stock, release_stock
from app.db import cursor
from app.policy import evaluate_policy, MERCHANT_POLICY
from tests.base import GatewayTestCase


class TestCatalog(GatewayTestCase):
    def test_list_catalog_seeds_and_returns_items(self):
        items = list_catalog()
        self.assertGreater(len(items), 0)

    def test_get_item_known_sku(self):
        item = get_item("SKU-BT-001")
        self.assertIsNotNone(item)
        self.assertEqual(item.name, "Wireless Earbuds Pro")

    def test_get_item_unknown_sku_returns_none(self):
        self.assertIsNone(get_item("SKU-DOES-NOT-EXIST"))


class TestPolicy(GatewayTestCase):
    def test_normal_order_within_policy_passes(self):
        item = get_item("SKU-KT-001")  # coffee mug, 599 rupees
        result = evaluate_policy(item, quantity=1, cart_total_paise=item.price_paise)
        self.assertTrue(result.passed)

    def test_order_exceeding_max_order_value_fails(self):
        item = get_item("SKU-GC-001")  # gift card 5000 rupees, over the 3000 cap
        result = evaluate_policy(item, quantity=1, cart_total_paise=item.price_paise)
        self.assertFalse(result.passed)
        self.assertIn("max_order_value", result.reason)

    def test_order_exceeding_per_item_limit_fails(self):
        item = get_item("SKU-KB-001")  # per_item_limit=2
        result = evaluate_policy(item, quantity=3, cart_total_paise=item.price_paise * 3)
        self.assertFalse(result.passed)
        self.assertIn("per-item limit", result.reason)

    def test_order_exceeding_merchant_wide_max_quantity_fails(self):
        item = get_item("SKU-BK-001")  # cheap, high per-item limit
        qty = MERCHANT_POLICY["max_quantity_per_mandate"] + 1
        result = evaluate_policy(item, quantity=qty, cart_total_paise=item.price_paise * qty)
        self.assertFalse(result.passed)
        self.assertIn("max_quantity_per_mandate", result.reason)

    def test_insufficient_stock_fails(self):
        # Construct a synthetic low-stock item directly: every real
        # catalog item's stock comfortably exceeds max_quantity_per_mandate,
        # so this path is only reachable via a low-stock item, which is
        # exactly what would happen once a real merchant's stock runs low.
        item = CatalogItem(
            sku="SKU-TEST-LOWSTOCK",
            name="Test Item",
            category="electronics",
            price_paise=1000,
            stock=1,
            per_item_limit=5,
        )
        result = evaluate_policy(item, quantity=2, cart_total_paise=2000)
        self.assertFalse(result.passed)
        self.assertIn("stock", result.reason)


class TestStockReservation(GatewayTestCase):
    """
    Regression tests for a real overselling bug found via testing:
    evaluate_policy()'s stock check is a plain read and can be stale
    under concurrency. Two DIFFERENT (non-duplicate, both legitimately
    signed) mandates for the very last unit of an item could both pass
    that check and both be accepted -- reproduced directly before this
    fix. reserve_stock() closes it the same way reserve_mandate() closes
    the mandate-identity race: a single atomic UPDATE ... WHERE clause,
    not a read-then-decide in Python.
    """

    def _seed_item(self, sku="SKU-STOCK-TEST", stock=1):
        with cursor() as cur:
            cur.execute(
                "INSERT INTO catalog (sku, name, category, price_paise, stock, per_item_limit) "
                "VALUES (?, 'Stock Test Item', 'home', 10000, ?, 10)",
                (sku, stock),
            )

    def test_reserve_stock_succeeds_and_decrements(self):
        self._seed_item("SKU-STOCK-A", stock=5)
        ok = reserve_stock("SKU-STOCK-A", 2)
        self.assertTrue(ok)
        self.assertEqual(get_item("SKU-STOCK-A").stock, 3)

    def test_reserve_stock_fails_when_insufficient(self):
        self._seed_item("SKU-STOCK-B", stock=1)
        ok = reserve_stock("SKU-STOCK-B", 5)
        self.assertFalse(ok)
        self.assertEqual(get_item("SKU-STOCK-B").stock, 1, "a failed reservation must not touch stock at all")

    def test_release_stock_restores_it(self):
        self._seed_item("SKU-STOCK-C", stock=5)
        reserve_stock("SKU-STOCK-C", 3)
        self.assertEqual(get_item("SKU-STOCK-C").stock, 2)
        release_stock("SKU-STOCK-C", 3)
        self.assertEqual(get_item("SKU-STOCK-C").stock, 5)

    def test_concurrent_reservations_for_last_unit_only_one_wins(self):
        """Direct proof of the fix: 20 simultaneous callers competing for
        a single unit of stock. Exactly one may succeed, stock must land
        at exactly 0, never negative. This is the same class of test as
        TestAtomicReserveConcurrency in test_gateway_pipeline.py, applied
        to inventory instead of mandate identity."""
        self._seed_item("SKU-STOCK-RACE", stock=1)

        results = []
        barrier = threading.Barrier(20)

        def attempt():
            barrier.wait()
            results.append(reserve_stock("SKU-STOCK-RACE", 1))

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(True), 1, "exactly one concurrent caller may win the last unit")
        self.assertEqual(results.count(False), 19)
        self.assertEqual(get_item("SKU-STOCK-RACE").stock, 0, "stock must never go negative")

    def test_two_different_agents_cannot_both_buy_the_last_unit_end_to_end(self):
        """The original bug, reproduced through the FULL pipeline (not
        just reserve_stock in isolation): two different, legitimately
        signed mandates for the last unit of stock. Before the fix,
        both were accepted."""
        from app import security
        from app.gateway import Mandate, process_mandate

        self._seed_item("SKU-STOCK-E2E", stock=1)
        item = get_item("SKU-STOCK-E2E")
        agent1 = security.register_agent("oversell-buyer-1")
        agent2 = security.register_agent("oversell-buyer-2")

        def make(agent, mandate_id):
            m = Mandate(
                mandate_id=mandate_id, agent_id=agent.agent_id, sku=item.sku, quantity=1,
                claimed_price_paise=item.price_paise, spend_limit_paise=item.price_paise,
                expires_at="2099-01-01T00:00:00+00:00", signature="",
            )
            m.signature = security.sign_mandate(agent.secret, m.payload())
            return m

        r1 = process_mandate(make(agent1, "mandate_oversell_e2e_1"))
        r2 = process_mandate(make(agent2, "mandate_oversell_e2e_2"))

        self.assertNotEqual(r1.accepted, r2.accepted, "exactly one of the two must be accepted, not both")
        self.assertEqual(get_item("SKU-STOCK-E2E").stock, 0)
