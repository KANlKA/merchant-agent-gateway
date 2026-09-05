"""
Standalone Razorpay credential check -- no app code involved at all.
If this fails with "Authentication failed" too, the problem is
definitely the .env values themselves, not anything in the gateway.
"""
import os
from dotenv import load_dotenv
load_dotenv()

key_id = os.environ.get("RAZORPAY_KEY_ID")
key_secret = os.environ.get("RAZORPAY_KEY_SECRET")

print(f"KEY_ID as loaded : {key_id!r}")
print(f"KEY_ID length    : {len(key_id) if key_id else 0}")
print(f"Starts with 'rzp_test_'? {key_id.startswith('rzp_test_') if key_id else False}")
print(f"KEY_SECRET length: {len(key_secret) if key_secret else 0}")
print()

import razorpay
client = razorpay.Client(auth=(key_id, key_secret))
try:
    order = client.order.create({"amount": 100, "currency": "INR", "receipt": "credential_test_1"})
    print("SUCCESS -- credentials are valid.")
    print(order)
except Exception as e:
    print("FAILED --", type(e).__name__, ":", e)