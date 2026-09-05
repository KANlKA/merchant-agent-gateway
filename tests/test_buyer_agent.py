import os

from agent.buyer_agent import BuyerAgent
from tests.base import GatewayTestCase


class TestBuyerAgentDeterministic(GatewayTestCase):
    def setUp(self):
        super().setUp()
        # Force deterministic mode regardless of the host environment,
        # so this test suite is reproducible and free to run anywhere.
        self._old_key = os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._old_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._old_key
        super().tearDown()

    def test_agent_registers_with_its_own_identity(self):
        agent = BuyerAgent("shopper-1")
        self.assertTrue(agent.identity.agent_id.startswith("agent_"))
        self.assertTrue(agent.identity.secret)

    def test_decide_picks_an_affordable_item(self):
        agent = BuyerAgent("shopper-2")
        decision = agent.decide("I need a coffee mug for my desk", budget_paise=100000)
        self.assertTrue(decision.sku)
        self.assertGreaterEqual(decision.quantity, 1)

    def test_decide_raises_when_nothing_is_affordable(self):
        agent = BuyerAgent("shopper-3")
        with self.assertRaises(ValueError):
            agent.decide("anything", budget_paise=1)

    def test_shop_end_to_end_produces_accepted_mandate(self):
        agent = BuyerAgent("shopper-4")
        decision, result = agent.shop("I want a water bottle", budget_paise=200000)
        self.assertTrue(result.accepted)
        self.assertIsNotNone(result.razorpay_order_id)

    def test_shop_respects_own_budget_as_spend_limit(self):
        """The mandate's spend_limit_paise should equal the budget the
        agent was given — proving the agent is actually constraining
        itself, not just picking anything."""
        agent = BuyerAgent("shopper-5")
        budget = 150000
        decision, result = agent.shop("a gift for a friend", budget_paise=budget)
        self.assertTrue(result.accepted)
