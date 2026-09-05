"""
Live end-to-end check against an ACTUAL RUNNING SERVER over real HTTP
(urllib, stdlib only — no in-process TestClient shortcuts). This is
the check that proves the FastAPI app, not just the underlying
gateway module, actually works.

Usage:
    # terminal 1
    uvicorn app.main:app --port 8000

    # terminal 2
    python scripts/e2e_http_check.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def _call(base_url: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def sign(secret: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    checks_passed = 0
    checks_total = 0

    def check(name, condition):
        nonlocal checks_passed, checks_total
        checks_total += 1
        status = "PASS" if condition else "FAIL"
        if condition:
            checks_passed += 1
        print(f"[{status}] {name}")

    print(f"Hitting live server at {base} over real HTTP...\n")

    status, health = _call(base, "GET", "/health")
    check("GET /health returns 200", status == 200)
    check("health reports razorpay_mode", "razorpay_mode" in health)
    print(f"   razorpay_mode = {health.get('razorpay_mode')}")

    status, catalog = _call(base, "GET", "/catalog")
    check("GET /catalog returns 200", status == 200)
    check("catalog is non-empty", isinstance(catalog, list) and len(catalog) > 0)

    status, agent = _call(base, "POST", "/agents/register", {"name": "http-e2e-test-agent"})
    check("POST /agents/register returns 200", status == 200)
    check("agent has agent_id and secret", "agent_id" in agent and "secret" in agent)

    item = catalog[0]
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    payload = {
        "mandate_id": f"mandate_http_e2e_{item['sku']}",
        "agent_id": agent["agent_id"],
        "sku": item["sku"],
        "quantity": 1,
        "claimed_price_paise": item["price_paise"],
        "spend_limit_paise": item["price_paise"],
        "expires_at": expires_at,
    }
    payload["signature"] = sign(agent["secret"], payload)

    status, result = _call(base, "POST", "/mandates/submit", payload)
    check("POST /mandates/submit returns 200", status == 200)
    check("valid mandate is accepted", result.get("accepted") is True)
    check("razorpay_order_id is present", bool(result.get("razorpay_order_id")))
    print(f"   order_id = {result.get('razorpay_order_id')}")

    # replay: submit the exact same mandate again
    status, replay_result = _call(base, "POST", "/mandates/submit", payload)
    check("replayed mandate is rejected", replay_result.get("accepted") is False)

    status, audit = _call(base, "GET", "/audit")
    check("GET /audit returns 200", status == 200)
    check("audit log contains this run's mandate", any(
        row["mandate_id"] == payload["mandate_id"] for row in audit
    ))

    print(f"\n{checks_passed}/{checks_total} checks passed")
    sys.exit(0 if checks_passed == checks_total else 1)


if __name__ == "__main__":
    main()
