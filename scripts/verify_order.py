
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.razorpay_client import KEY_ID, KEY_SECRET, LIVE_MODE  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/verify_order.py <order_id>")
        sys.exit(1)

    order_id = sys.argv[1]

    if not LIVE_MODE:
        print("LIVE_MODE is False -- RAZORPAY_KEY_ID/SECRET aren't set (or the razorpay")
        print("package isn't installed), so there's nothing real to verify.")
        print("Set both in your environment and re-run.")
        sys.exit(1)

    if order_id.startswith("order_MOCK"):
        print(f"'{order_id}' is a MOCK order id -- it was never sent to Razorpay,")
        print("so there's nothing to fetch. Re-run run_demo.py with LIVE_MODE on")
        print("first to get a real order_id.")
        sys.exit(1)

    import razorpay

    client = razorpay.Client(auth=(KEY_ID, KEY_SECRET))

    print(f"Fetching {order_id} directly from Razorpay's API (not from our own DB)...\n")
    try:
        order = client.order.fetch(order_id)
    except Exception as e:
        print(f"Razorpay rejected the fetch: {e}")
        sys.exit(1)

    print("Razorpay's API confirms:")
    print(f"  id          : {order['id']}")
    print(f"  status      : {order['status']}")
    print(f"  amount      : {order['amount']} paise (₹{order['amount'] / 100:.2f})")
    print(f"  currency    : {order['currency']}")
    print(f"  receipt     : {order.get('receipt')}")
    print(f"  created_at  : {order['created_at']}  (unix timestamp)")
    print(f"  notes       : {order.get('notes')}")
    print("\nThis came straight from Razorpay's servers, not our gateway or our database.")


if __name__ == "__main__":
    main()