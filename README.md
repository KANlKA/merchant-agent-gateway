# Merchant Agent Gateway

A hardened, tested reference for what a **merchant-side integration**
against agentic-commerce infrastructure (Google's AP2, OpenAI/Stripe's
ACP, NPCI's Unified Agent Protocol for UPI) needs to get right —
demonstrated end-to-end against Razorpay.

Built for the Razorpay agentic-commerce hackathon.

Not "build agentic commerce" — the trust/mandate/signing pattern
already exists (AP2, Razorpay's own NPCI/OpenAI initiative). This
project hardens that pattern to production-grade rigor:

- **Per-agent credential isolation** — every buyer agent gets its own
  secret at registration; no shared merchant-wide key.
- **An independent merchant policy gate** — a buyer agent's own claims
  about its spending authority cannot satisfy the merchant's policy on
  its own behalf. Two separate gates; passing one never satisfies the
  other.
- **Atomic transaction safety** — a real concurrency bug (double-charge
  race condition) was found via deliberate stress-testing and fixed;
  the regression test is in `tests/test_gateway_pipeline.py`.
- **A full audit trail** — every attempt, accepted or rejected, is
  logged with both gates' results and a specific reason. Never a
  silent failure and never a charge without a logged reason.

## Architecture

```
Buyer Agent (agent/buyer_agent.py)
   │  reasons over goal+budget, picks item, signs its own mandate
   ▼
POST /mandates/submit (app/main.py, FastAPI)
   ▼
app/gateway.py — process_mandate()
   1. reserve_mandate()   — ATOMIC claim via DB UNIQUE constraint
   2. verify_mandate()    — signature, expiry, live price, own spend limit
   3. evaluate_policy()   — INDEPENDENT merchant policy gate
   4. razorpay_client.create_order() — real test-mode order, or mock
   5. audit_log write     — every attempt, pass or fail
```

Core logic (`app/`) is deliberately dependency-free (stdlib `sqlite3`,
`hmac`, `hashlib` only) so it's fully unit-testable without a server.
FastAPI (`app/main.py`) is a thin HTTP adapter on top of it.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 1. Zero-setup live demo (no server needed)

```bash
python scripts/run_demo.py
```

Registers a real buyer agent, gives it a goal + budget, watches it
reason/sign/submit a mandate, then demonstrates a blocked forgery
attack and a blocked replay attack, and prints the audit trail. Runs
in mock Razorpay mode automatically if no keys are set.

### 2. Run the API server

```bash
uvicorn app.main:app --reload --port 8000
```

Docs at `http://localhost:8000/docs`.

### 3. Prove it end-to-end over real HTTP

```bash
# separate terminal, server must be running
python scripts/e2e_http_check.py --base-url http://localhost:8000
```

Registers an agent, submits a mandate, replays it, checks the audit
log — all via real HTTP requests (`urllib`, not an in-process test
client), against the actual running server.

### 4. Run the dashboard

```bash
streamlit run dashboard/streamlit_app.py
```

Live catalog, a "run an agent" panel (including a one-click forged-
mandate attack demo), a filterable audit trail with an outcome
breakdown chart, and a one-click batch evaluation runner.

### 5. Tests

```bash
pytest                          # or: python -m unittest discover -s tests
```

37 tests: signing/credential isolation, catalog, merchant policy,
the full accept/reject pipeline, the Razorpay mock client, the buyer
agent, and — the important one — a real multi-threaded concurrency
regression test proving the atomic reserve prevents a duplicate
mandate from ever creating two orders.

### 6. Batch evaluation

```bash
python scripts/batch_eval.py
```

83 synthetic mandates covering every accept/reject scenario the
pipeline supports (clean orders, forged signatures, expired mandates,
stale prices, over-policy orders, replay attacks, zero/negative
quantities, and more), run through the real `process_mandate()`
pipeline and scored against expected outcomes with a full confusion
matrix. **False accepts** (something that should have been rejected
wasn't — the dangerous kind) are reported separately from **false
rejects** (something safe got blocked).

## Enabling real Razorpay test-mode

By default the system runs in **mock mode** — no setup required, the
whole pipeline works end-to-end, Razorpay order IDs look like
`order_MOCK...`. To hit Razorpay's actual sandbox API:

```bash
cp .env.example .env
# fill in RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from your Razorpay
# test-mode dashboard, then:
export $(cat .env | xargs)
uvicorn app.main:app --reload
```

`GET /health` reports which mode is active. Real orders get IDs in
Razorpay's own `order_<id>` shape and are visible in your Razorpay
test-mode dashboard.

## Enabling Claude-based agent reasoning

By default the buyer agent uses a free, deterministic, offline
keyword-scoring heuristic to pick items (this is also what
`batch_eval.py` always uses, so evaluation is reproducible). Set
`ANTHROPIC_API_KEY` to have the agent reason with Claude instead —
it'll fall back to the heuristic automatically on any API error, so a
demo never breaks on a network hiccup.

## Deploying

`render.yaml` deploys both the API and the dashboard as separate
Render web services on the free tier. Set the env vars in the Render
dashboard (never commit real keys).

## Fixed during review (a second real bug, found the same way as the first)

A follow-up review of this codebase found that `evaluate_policy()`'s
stock check was a plain read, not atomic — meaning two *different*,
legitimately signed mandates for the last unit of a low-stock item
could both pass it and both be accepted, since neither write had
happened yet when either check ran. Reproduced directly: a stock=1
item, two different buyers, both orders created, stock never
decremented. The same class of bug as the mandate-replay race already
documented above, just on inventory instead of payment identity.

Fixed the same way: `catalog.reserve_stock()` does the decrement as a
single atomic `UPDATE ... WHERE stock >= quantity`, so only one
concurrent caller can ever win the last unit — proven directly with a
20-way concurrent race in
`tests/test_catalog_and_policy.py::TestStockReservation`. If order
creation then fails, the reservation is released via
`catalog.release_stock()` rather than permanently stranding inventory
on a failed payment — also covered by a dedicated test.

Separately, `razorpay_client.py`'s docstring claimed "the audit log
always records which mode created the order," but `audit_log` had no
such column and the value was silently discarded. Added a
`razorpay_mode` column, threaded it through `GatewayResult` and the
API response, and added a regression test
(`test_audit_log_records_which_razorpay_mode_created_the_order`) that
actually checks the claim rather than trusting the comment.

## What's out of scope (v1)

- Multi-merchant support — single hardcoded merchant/catalog/policy.
- Asymmetric cryptographic mandate signing (AP2's full spec) —
  simplified to per-agent HMAC secrets, same shape, swappable later.
- A conversational, multi-turn checkout UI — the agent takes one
  plain-English goal and reasons once.

## A note on this repo's origin

This codebase was built from scratch in a sandboxed environment with
no internet access, which meant `fastapi`/`streamlit`/`razorpay`
couldn't be installed or run there. To still prove correctness before
handing this over, the core pipeline (`app/gateway.py`,
`app/security.py`, `app/policy.py`, `app/catalog.py`,
`app/razorpay_client.py`, `agent/buyer_agent.py`) was written to be
dependency-free, and was actually run in that sandbox: **all 37 unit/
integration tests pass, the 83-case batch evaluation scores 100% with
zero false accepts, and `scripts/run_demo.py` runs live end-to-end**
(a real agent registers, reasons, signs a mandate, gets it accepted
with a mock Razorpay order — and a forged mandate plus a replayed
mandate are both correctly blocked). The FastAPI/Streamlit layers
(`app/main.py`, `dashboard/streamlit_app.py`) and the real-HTTP check
(`scripts/e2e_http_check.py`) are complete and syntax-checked, but
need `pip install -r requirements.txt` on a machine with internet
access to actually run — do that first thing and re-run
`e2e_http_check.py` to get the real-HTTP proof on record too.
