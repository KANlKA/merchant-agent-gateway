"""
Per-agent credential isolation.

Each buyer agent gets its OWN secret at registration time — never a
shared merchant-wide secret. A mandate is only valid if it verifies
against the specific secret of the agent_id it claims to be from, so
one agent cannot forge a mandate on behalf of another, and a leaked
secret compromises exactly one agent, not the whole gateway.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.db import cursor


@dataclass
class Agent:
    agent_id: str
    name: str
    secret: str


def register_agent(name: str) -> Agent:
    agent_id = f"agent_{uuid.uuid4().hex[:16]}"
    secret = secrets.token_hex(32)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO agents (agent_id, name, secret) VALUES (?, ?, ?)",
            (agent_id, name, secret),
        )
    return Agent(agent_id=agent_id, name=name, secret=secret)


def get_agent(agent_id: str) -> Optional[Agent]:
    with cursor() as cur:
        cur.execute("SELECT agent_id, name, secret FROM agents WHERE agent_id = ?", (agent_id,))
        row = cur.fetchone()
        return Agent(**dict(row)) if row else None


def _canonical(payload: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding so signer and verifier hash the same
    bytes regardless of dict insertion order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_mandate(secret: str, payload: dict[str, Any]) -> str:
    mac = hmac.new(secret.encode(), _canonical(payload), hashlib.sha256)
    return mac.hexdigest()


def verify_signature(secret: str, payload: dict[str, Any], signature: str) -> bool:
    expected = sign_mandate(secret, payload)
    # constant-time compare — don't leak timing info about the secret
    return hmac.compare_digest(expected, signature)
