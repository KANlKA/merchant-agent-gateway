"""
FastAPI HTTP layer. Thin by design: every endpoint just validates
shape via Pydantic and delegates to app.gateway / app.security /
app.catalog, which are the actually-tested, framework-agnostic core.

Run locally:
    pip install -r requirements.txt
    uvicorn app.main:app --reload --port 8000

Docs at http://localhost:8000/docs
"""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app import security
from app.catalog import list_catalog
from app.gateway import Mandate, get_audit_log, process_mandate
from app.razorpay_client import LIVE_MODE

app = FastAPI(
    title="Merchant Agent Gateway",
    description=(
        "Reference merchant-side integration for agentic commerce "
        "(AP2 / ACP / UPI Agent Protocol-style flows) against Razorpay. "
        "Hardens the catalog -> mandate -> verify -> policy -> pay pattern "
        "with per-agent credential isolation, an independent policy gate, "
        "atomic transaction safety, and a full audit trail."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- schemas ----------

class RegisterAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class RegisterAgentResponse(BaseModel):
    agent_id: str
    name: str
    secret: str
    warning: str = "Store this secret now — it is not retrievable again and is required to sign mandates."


class MandateRequest(BaseModel):
    mandate_id: str
    agent_id: str
    sku: str
    quantity: int
    claimed_price_paise: int
    spend_limit_paise: int
    expires_at: str
    signature: str


class MandateResponse(BaseModel):
    mandate_id: str
    accepted: bool
    final_status: str
    verification_result: str
    verification_reason: str
    policy_result: str
    policy_reason: str
    razorpay_order_id: Optional[str] = None
    razorpay_mode: Optional[str] = None


# ---------- endpoints ----------

@app.get("/health")
def health():
    return {"status": "ok", "razorpay_mode": "live_test_mode" if LIVE_MODE else "mock"}


@app.post("/agents/register", response_model=RegisterAgentResponse)
def register_agent(req: RegisterAgentRequest):
    agent = security.register_agent(req.name)
    return RegisterAgentResponse(agent_id=agent.agent_id, name=agent.name, secret=agent.secret)


@app.get("/catalog")
def catalog():
    return [item.__dict__ for item in list_catalog()]


@app.post("/mandates/submit", response_model=MandateResponse)
def submit_mandate(req: MandateRequest):
    mandate = Mandate(**req.model_dump())
    result = process_mandate(mandate)
    return MandateResponse(
        mandate_id=result.mandate_id,
        accepted=result.accepted,
        final_status=result.final_status,
        verification_result=result.verification_result,
        verification_reason=result.verification_reason,
        policy_result=result.policy_result,
        policy_reason=result.policy_reason,
        razorpay_order_id=result.razorpay_order_id,
        razorpay_mode=result.razorpay_mode,
    )


@app.get("/audit")
def audit(limit: int = 200):
    return get_audit_log(limit=limit)
