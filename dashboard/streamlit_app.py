"""
Merchant Agent Gateway — Streamlit dashboard.

Run:
    pip install -r requirements.txt
    streamlit run dashboard/streamlit_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from agent.buyer_agent import BuyerAgent
from app.catalog import list_catalog
from app.gateway import get_audit_log
from app.razorpay_client import LIVE_MODE
from scripts.batch_eval import run_evaluation

st.set_page_config(page_title="Merchant Agent Gateway", page_icon="🛡️", layout="wide")

st.title("🛡️ Merchant Agent Gateway")
st.caption(
    "Reference merchant-side integration for agentic commerce (AP2 / ACP / UPI Agent "
    "Protocol-style flows) against Razorpay — with per-agent credential isolation, an "
    "independent merchant policy gate, atomic transaction safety, and a full audit trail."
)

mode_badge = "🟢 Razorpay LIVE TEST MODE" if LIVE_MODE else "🟡 MOCK mode (no Razorpay keys set)"
st.markdown(f"**Payment backend:** {mode_badge}")

tab_catalog, tab_agent, tab_audit, tab_eval = st.tabs(
    ["📦 Catalog", "🤖 Run an Agent", "📋 Audit Trail", "🧪 Batch Evaluation"]
)

# ---------------- Catalog tab ----------------
with tab_catalog:
    st.subheader("Live merchant catalog")
    items = list_catalog()
    df = pd.DataFrame([{
        "SKU": i.sku, "Name": i.name, "Category": i.category,
        "Price (₹)": i.price_paise / 100, "Stock": i.stock, "Per-item limit": i.per_item_limit,
    } for i in items])
    st.dataframe(df, use_container_width=True, hide_index=True)

# ---------------- Agent tab ----------------
with tab_agent:
    st.subheader("Spin up a real buyer agent")
    st.write(
        "This registers a brand-new agent identity (its own secret, never shared), "
        "gives it a plain-English goal and a budget, and lets it reason, build, sign, "
        "and submit its own mandate through the real gateway pipeline."
    )
    col1, col2 = st.columns([3, 1])
    with col1:
        goal = st.text_input("Shopping goal", value="I need a comfortable pair of shoes for running")
    with col2:
        budget_rupees = st.number_input("Budget (₹)", min_value=1, value=3000, step=100)

    if st.button("Run agent", type="primary"):
        with st.spinner("Agent reasoning, signing mandate, submitting to gateway..."):
            agent = BuyerAgent(f"dashboard-agent-{pd.Timestamp.now().value}")
            try:
                decision, result = agent.shop(goal, budget_paise=int(budget_rupees * 100))
                st.success(f"Decision: {decision.quantity}x **{decision.sku}** — {decision.rationale}")
                cols = st.columns(4)
                cols[0].metric("Verification", result.verification_result)
                cols[1].metric("Policy", result.policy_result)
                cols[2].metric("Final status", result.final_status)
                cols[3].metric("Razorpay order", result.razorpay_order_id or "—")
                if not result.accepted:
                    st.warning(f"Rejected — verification: {result.verification_reason} | policy: {result.policy_reason}")
            except ValueError as e:
                st.error(str(e))

    st.divider()
    st.write("**Try an attack scenario:**")
    if st.button("Simulate a forged mandate (should be blocked)"):
        from app import security
        from app.gateway import Mandate, process_mandate

        victim = BuyerAgent("dashboard-victim")
        attacker = security.register_agent("dashboard-attacker")
        item = list_catalog()[0]
        forged = Mandate(
            mandate_id=f"mandate_dashboard_forgery_{pd.Timestamp.now().value}",
            agent_id=victim.identity.agent_id, sku=item.sku, quantity=1,
            claimed_price_paise=item.price_paise, spend_limit_paise=item.price_paise,
            expires_at="2099-01-01T00:00:00+00:00", signature="",
        )
        forged.signature = security.sign_mandate(attacker.secret, forged.payload())
        result = process_mandate(forged)
        if result.accepted:
            st.error("Forged mandate was ACCEPTED — this would be a critical bug.")
        else:
            st.success(f"Forged mandate correctly rejected: {result.verification_reason}")

# ---------------- Audit tab ----------------
with tab_audit:
    st.subheader("Audit trail")
    log = get_audit_log(limit=500)
    if not log:
        st.info("No attempts logged yet — run an agent from the 'Run an Agent' tab.")
    else:
        log_df = pd.DataFrame(log)
        c1, c2 = st.columns(2)
        with c1:
            status_filter = st.multiselect(
                "Filter by final status", options=sorted(log_df["final_status"].unique()),
                default=list(log_df["final_status"].unique()),
            )
        with c2:
            search = st.text_input("Search mandate_id / sku / agent_id", value="")

        filtered = log_df[log_df["final_status"].isin(status_filter)]
        if search:
            mask = (
                filtered["mandate_id"].str.contains(search, case=False, na=False)
                | filtered["sku"].astype(str).str.contains(search, case=False, na=False)
                | filtered["agent_id"].astype(str).str.contains(search, case=False, na=False)
            )
            filtered = filtered[mask]

        st.write(f"Showing {len(filtered)} of {len(log_df)} attempts")
        st.dataframe(filtered, use_container_width=True, hide_index=True)

        st.write("**Outcome breakdown**")
        breakdown = log_df["final_status"].value_counts()
        st.bar_chart(breakdown)

# ---------------- Batch eval tab ----------------
with tab_eval:
    st.subheader("Batch evaluation")
    st.write(
        "Runs the full synthetic case suite (every accept/reject scenario the pipeline "
        "supports) through the real gateway and scores actual vs. expected outcome."
    )
    if st.button("Run batch evaluation", type="primary"):
        with st.spinner("Running evaluation cases through the gateway..."):
            eval_db = Path(__file__).resolve().parent.parent / "data" / "dashboard_eval.db"
            report = run_evaluation(eval_db)

        rows = report["rows"]
        confusion = report["confusion"]
        n = report["n_cases"]
        correct = sum(1 for r in rows if r["correct"])

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Cases", n)
        c2.metric("Accuracy", f"{correct/n:.0%}")
        c3.metric("True accepts", confusion["true_accept"])
        c4.metric("True rejects", confusion["true_reject"])
        c5.metric("False accepts ⚠️", confusion["false_accept"])

        if confusion["false_accept"] > 0:
            st.error(f"{confusion['false_accept']} false accept(s) detected — review immediately.")
        else:
            st.success("No false accepts. All rejection scenarios correctly blocked.")

        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
