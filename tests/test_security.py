from app import security
from tests.base import GatewayTestCase


class TestAgentRegistration(GatewayTestCase):
    def test_register_returns_unique_id_and_secret(self):
        a1 = security.register_agent("agent-one")
        a2 = security.register_agent("agent-two")
        self.assertNotEqual(a1.agent_id, a2.agent_id)
        self.assertNotEqual(a1.secret, a2.secret)
        self.assertTrue(a1.agent_id.startswith("agent_"))

    def test_get_agent_roundtrip(self):
        a1 = security.register_agent("agent-one")
        fetched = security.get_agent(a1.agent_id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.secret, a1.secret)

    def test_get_unknown_agent_returns_none(self):
        self.assertIsNone(security.get_agent("agent_does_not_exist"))


class TestSigning(GatewayTestCase):
    def test_valid_signature_verifies(self):
        payload = {"a": 1, "b": "x"}
        sig = security.sign_mandate("secret123", payload)
        self.assertTrue(security.verify_signature("secret123", payload, sig))

    def test_signature_is_order_independent(self):
        sig1 = security.sign_mandate("s", {"a": 1, "b": 2})
        sig2 = security.sign_mandate("s", {"b": 2, "a": 1})
        self.assertEqual(sig1, sig2)

    def test_tampered_payload_fails_verification(self):
        payload = {"amount": 100}
        sig = security.sign_mandate("secret", payload)
        tampered = {"amount": 100000}
        self.assertFalse(security.verify_signature("secret", tampered, sig))

    def test_wrong_secret_fails_verification(self):
        payload = {"amount": 100}
        sig = security.sign_mandate("secret-A", payload)
        self.assertFalse(security.verify_signature("secret-B", payload, sig))

    def test_cross_agent_forgery_is_rejected(self):
        """The core credential-isolation property: agent B cannot forge
        a mandate that verifies as agent A's, even knowing A's other
        public details, because it doesn't have A's secret."""
        agent_a = security.register_agent("A")
        agent_b = security.register_agent("B")
        payload = {"agent_id": agent_a.agent_id, "sku": "SKU-1", "amount": 100}
        # B signs a mandate claiming to be A
        forged_sig = security.sign_mandate(agent_b.secret, payload)
        self.assertFalse(security.verify_signature(agent_a.secret, payload, forged_sig))
