from app.razorpay_client import create_order, LIVE_MODE
from tests.base import GatewayTestCase


class TestRazorpayClientMock(GatewayTestCase):
    def test_mock_mode_active_without_keys(self):
        # In this test environment no RAZORPAY_KEY_ID/SECRET are set,
        # so the client must be in mock mode, not silently attempting
        # real network calls.
        self.assertFalse(LIVE_MODE)

    def test_create_order_returns_expected_shape(self):
        order = create_order(amount_paise=50000, receipt="mandate_test_1")
        self.assertEqual(order.mode, "mock")
        self.assertTrue(order.order_id.startswith("order_"))
        self.assertEqual(order.amount_paise, 50000)
        self.assertEqual(order.currency, "INR")
        self.assertEqual(order.status, "created")

    def test_create_order_ids_are_unique(self):
        o1 = create_order(amount_paise=100, receipt="r1")
        o2 = create_order(amount_paise=100, receipt="r2")
        self.assertNotEqual(o1.order_id, o2.order_id)


class TestDotenvLoading(GatewayTestCase):
    """
    Regression test for a real gap found in practice: .env.example
    implied dotenv support, but nothing in the codebase actually called
    load_dotenv(), so creating a .env file silently did nothing and the
    gateway stayed in mock mode even with real keys present on disk.
    Run in a subprocess because razorpay_client reads the environment
    at MODULE IMPORT time -- this test needs a fresh Python process
    with a real .env file on disk, not a monkeypatched os.environ in
    the already-imported test process.
    """

    def test_dotenv_file_is_actually_loaded_into_live_mode(self):
        import subprocess
        import tempfile
        import os as _os
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "RAZORPAY_KEY_ID=rzp_test_regression_check\n"
                "RAZORPAY_KEY_SECRET=fake_secret_for_test\n"
            )
            project_root = Path(__file__).resolve().parent.parent
            clean_env = {k: v for k, v in _os.environ.items()
                         if k not in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")}
            result = subprocess.run(
                ["python3", "-c",
                 "from app.razorpay_client import LIVE_MODE, KEY_ID; "
                 "print(LIVE_MODE); print(KEY_ID)"],
                cwd=tmp,
                env={**clean_env, "PYTHONPATH": str(project_root)},
                capture_output=True, text=True,
            )
            lines = result.stdout.strip().splitlines()
            self.assertEqual(lines[0], "True",
                              f".env was not picked up -- stdout={result.stdout!r} stderr={result.stderr!r}")
            self.assertEqual(lines[1], "rzp_test_regression_check")