"""
The buyer agent. This is a REAL agent in the sense that matters for
this project: it takes a plain-English goal + a budget, reasons about
which catalog item best satisfies the goal, decides a quantity, signs
its own mandate with its own registered secret, and submits it to the
gateway — with no special access or trust beyond what its signature
proves.

Two reasoning modes:
  - Claude mode (default if ANTHROPIC_API_KEY is set): sends the goal +
    live catalog to Claude and asks it to pick a SKU + quantity + a
    short rationale, returned as strict JSON.
  - Deterministic mode (always available, zero cost, used by
    scripts/batch_eval.py so evaluation is reproducible): a keyword/
    category scoring heuristic over the live catalog.

Either way, the agent itself decides its own spend_limit_paise (it can
claim anything) — that claim is exactly what the merchant's
independent policy gate does NOT have to trust. That asymmetry is the
point of the project.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app import security
from app.catalog import CatalogItem, list_catalog
from app.gateway import Mandate, GatewayResult, process_mandate

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")


@dataclass
class AgentDecision:
    sku: str
    quantity: int
    rationale: str


class BuyerAgent:
    """One instance = one registered agent identity with its own secret."""

    def __init__(self, name: str):
        self.identity = security.register_agent(name)

    # ---------- reasoning ----------

    def decide(self, goal: str, budget_paise: int) -> AgentDecision:
        catalog = list_catalog()
        affordable = [c for c in catalog if c.price_paise <= budget_paise]
        if not affordable:
            cheapest = min(catalog, key=lambda c: c.price_paise)
            raise ValueError(
                f"No catalog item fits budget of {budget_paise} paise; "
                f"cheapest item is {cheapest.sku} at {cheapest.price_paise} paise"
            )

        if ANTHROPIC_API_KEY:
            try:
                return self._decide_with_claude(goal, budget_paise, affordable)
            except Exception:
                pass  # fall through to deterministic mode — never let a demo break on API hiccups

        return self._decide_deterministic(goal, budget_paise, affordable)

    def _decide_with_claude(
        self, goal: str, budget_paise: int, affordable: list[CatalogItem]
    ) -> AgentDecision:
        import urllib.request

        catalog_json = [
            {"sku": c.sku, "name": c.name, "category": c.category, "price_paise": c.price_paise}
            for c in affordable
        ]
        prompt = (
            f"You are a shopping agent. Goal: {goal!r}. Budget: {budget_paise} paise.\n"
            f"Catalog (affordable items only):\n{json.dumps(catalog_json)}\n\n"
            "Pick exactly one sku and a quantity (integer >= 1) that best satisfies the "
            "goal within budget. Respond with ONLY minified JSON, no prose, no markdown: "
            '{"sku": "...", "quantity": N, "rationale": "one short sentence"}'
        )
        body = json.dumps(
            {
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        text = "".join(block.get("text", "") for block in data.get("content", []))
        text = text.strip().strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        parsed = json.loads(text)
        chosen_skus = {c.sku for c in affordable}
        if parsed["sku"] not in chosen_skus:
            raise ValueError(f"Claude picked a sku not in the affordable set: {parsed['sku']}")
        return AgentDecision(
            sku=parsed["sku"], quantity=int(parsed["quantity"]), rationale=parsed.get("rationale", "")
        )

    def _decide_deterministic(
        self, goal: str, budget_paise: int, affordable: list[CatalogItem]
    ) -> AgentDecision:
        """Simple, reproducible keyword-overlap scoring. No network calls
        — this is what makes batch_eval.py deterministic and free."""
        goal_words = set(goal.lower().replace("-", " ").split())

        def score(item: CatalogItem) -> tuple[int, int]:
            name_words = set(item.name.lower().replace("-", " ").split())
            cat_words = set(item.category.lower().replace("-", " ").split())
            overlap = len(goal_words & (name_words | cat_words))
            # tie-break: prefer items that use MORE of the budget (better fit)
            # without exceeding it — a rough proxy for "best value pick".
            return (overlap, item.price_paise)

        best = max(affordable, key=score)
        overlap_score = score(best)[0]
        rationale = (
            f"Best keyword match for goal within budget (score={overlap_score})"
            if overlap_score > 0
            else "No strong keyword match; picked the highest-value item within budget"
        )
        return AgentDecision(sku=best.sku, quantity=1, rationale=rationale)

    # ---------- mandate construction + submission ----------

    def build_mandate(
        self,
        decision: AgentDecision,
        spend_limit_paise: int,
        ttl_seconds: int = 300,
        _price_override: Optional[int] = None,
        _mandate_id_override: Optional[str] = None,
    ) -> Mandate:
        item = next(c for c in list_catalog() if c.sku == decision.sku)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
        mandate_id = _mandate_id_override or f"mandate_{uuid.uuid4().hex}"

        m = Mandate(
            mandate_id=mandate_id,
            agent_id=self.identity.agent_id,
            sku=decision.sku,
            quantity=decision.quantity,
            claimed_price_paise=_price_override if _price_override is not None else item.price_paise,
            spend_limit_paise=spend_limit_paise,
            expires_at=expires_at,
            signature="",  # filled below
        )
        m.signature = security.sign_mandate(self.identity.secret, m.payload())
        return m

    def shop(self, goal: str, budget_paise: int) -> tuple[AgentDecision, GatewayResult]:
        """End-to-end: reason -> build+sign mandate -> submit to gateway."""
        decision = self.decide(goal, budget_paise)
        mandate = self.build_mandate(decision, spend_limit_paise=budget_paise)
        result = process_mandate(mandate)
        return decision, result
