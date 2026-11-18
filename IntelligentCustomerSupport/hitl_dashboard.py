"""
Human-in-the-Loop (HITL) Supervisor Dashboard
Supports both interactive Streamlit Web UI (as requested in Lab Variation 7)
and interactive Terminal CLI mode for reviewing, editing, approving, and escalating tickets.
"""

import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

from ticket_manager import (
    init_db, get_ticket, save_ticket, apply_review_action, 
    get_db_connection, ALLOWED_ACTIONS
)
from sla_monitor import evaluate_sla_status
from audit_logger import log_event
from notifications import generate_notification, send_with_retry

def get_pending_review_tickets(db_path: str = "tickets.db") -> List[Dict[str, Any]]:
    """Fetches all tickets requiring human-in-the-loop attention."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        rows = conn.execute("""
            SELECT * FROM tickets 
            WHERE status IN ('PENDING_APPROVAL', 'IN_REVIEW', 'ESCALATED', 'AWAITING_CUSTOMER', 'OPEN')
            ORDER BY 
                CASE priority 
                    WHEN 'P1' THEN 1 
                    WHEN 'P2' THEN 2 
                    WHEN 'P3' THEN 3 
                    ELSE 4 
                END, created_at ASC
        """).fetchall()
        
        tickets = []
        for r in rows:
            t = dict(r)
            sla_state, mins = evaluate_sla_status(t)
            t["sla_state"] = sla_state
            t["sla_remaining_minutes"] = round(mins, 1)
            tickets.append(t)
        return tickets

def execute_review(ticket_id: str, action: str, reviewer_id: str, 
                   edited_text: str = None) -> Dict[str, Any]:
    """Applies a review action with security authorization and dispatches notifications."""
    cfg_path = Path("config.json")
    authorized_reviewers = None
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                authorized_reviewers = cfg.get("authorized_reviewers")
        except Exception:
            pass

    ticket = get_ticket(ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found.")

    updated = apply_review_action(
        ticket=ticket,
        action=action,
        reviewer_id=reviewer_id,
        authorized_reviewers=authorized_reviewers,
        edited_response=edited_text
    )
    save_ticket(updated)

    log_event(
        query_id=updated.get("query_id", "N/A"),
        ticket_id=ticket_id,
        event_type=f"HITL_{action}_DECISION",
        ticket_status=updated["status"],
        details=f"Human reviewer '{reviewer_id}' executed '{action}'."
    )

    if action in ("APPROVE", "RESOLVE"):
        notif = generate_notification(updated, "RESOLVED")
        send_with_retry(notif, lambda: True, max_attempts=3, backoff_seconds=[0, 0, 0])
    elif action == "ESCALATE":
        notif = generate_notification(updated, "ESCALATED")
        send_with_retry(notif, lambda: True, max_attempts=3, backoff_seconds=[0, 0, 0])

    return updated


# ---------------------------------------------------------------------------
# Streamlit Web Dashboard Mode
# ---------------------------------------------------------------------------

def run_streamlit_app():
    import streamlit as st

    st.set_page_config(page_title="Banking Dispute & Fraud HITL Portal", page_icon="🏦", layout="wide")
    st.title("🏦 Banking Dispute & Fraud Triage — Human-in-the-Loop Supervisor Portal")
    st.markdown("Review AI classification, verify knowledge grounding, approve automated drafts, or escalate high-risk cases.")

    # Sidebar: Reviewer Authentication
    st.sidebar.header("🔐 Reviewer Authentication")
    reviewer_id = st.sidebar.selectbox(
        "Select Active Supervisor Identity:",
        ["REV_AGENT_01", "REV_LEAD_02", "REV_FRAUD_OFFICER_03", "UNAUTHORIZED_GUEST"]
    )
    st.sidebar.caption("Role-Based Access Control (RBAC) enforced via config.json")

    tickets = get_pending_review_tickets()

    # Metric Row
    col1, col2, col3, col4 = st.columns(4)
    p1_count = sum(1 for t in tickets if t.get("priority") == "P1")
    at_risk_count = sum(1 for t in tickets if t.get("sla_state") == "AT_RISK")
    breached_count = sum(1 for t in tickets if t.get("sla_state") == "BREACHED")
    pending_appr = sum(1 for t in tickets if t.get("status") == "PENDING_APPROVAL")

    col1.metric("Critical P1 In Queue", p1_count, delta_color="inverse")
    col2.metric("Pending Approvals", pending_appr)
    col3.metric("SLA At Risk", at_risk_count)
    col4.metric("SLA Breached", breached_count, delta_color="inverse")

    st.divider()

    if not tickets:
        st.success("🎉 All customer dispute tickets have been reviewed! Inbox zero.")
        return

    # Select ticket to review
    ticket_options = [
        f"[{t['priority']}] {t['ticket_id']} - {t['intent']} (SLA: {t['sla_state']})" 
        for t in tickets
    ]
    selected_label = st.selectbox("Select Ticket from Review Queue:", ticket_options)
    selected_idx = ticket_options.index(selected_label)
    ticket = tickets[selected_idx]

    # Two column layout for ticket details and actions
    left_col, right_col = st.columns([1.2, 1])

    with left_col:
        st.subheader("📋 Ticket Metadata & AI Diagnostics")
        st.write(f"**Ticket ID:** `{ticket['ticket_id']}` | **Customer ID:** `{ticket['customer_id']}`")
        st.write(f"**Intent:** `{ticket['intent']}` | **Complexity:** `{ticket['complexity']}` | **Priority:** `{ticket['priority']}`")
        st.write(f"**Current Status:** `{ticket['status']}` | **Assigned Queue:** `{ticket['assigned_team']}`")
        st.write(f"**First Response Due:** `{ticket['first_response_due']}`")
        
        sla_color = "red" if ticket["sla_state"] == "BREACHED" else ("orange" if ticket["sla_state"] == "AT_RISK" else "green")
        st.markdown(f"**Live SLA State:** :{sla_color}[**{ticket['sla_state']}**] ({ticket['sla_remaining_minutes']} mins remaining)")

        st.info(f"**Proposed AI Resolution / KB Summary:**\n\n{ticket.get('resolution_summary') or 'None'}")

    with right_col:
        st.subheader("✍️ Human-in-the-Loop Actions")
        edited_text = st.text_area("Edit Response Draft (if modifying):", value=ticket.get("resolution_summary") or "", height=120)

        act_col1, act_col2 = st.columns(2)
        with act_col1:
            if st.button("✅ Approve Draft", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "APPROVE", reviewer_id)
                    st.success("Response approved and dispatched to customer!")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.button("📝 Save Edited Response", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "EDIT", reviewer_id, edited_text)
                    st.success("Edited response saved into audit history.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.button("⏸️ Hold Ticket", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "HOLD", reviewer_id)
                    st.warning("Ticket placed ON_HOLD (SLA clock paused).")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        with act_col2:
            if st.button("🚨 Escalate to Tier-3 Fraud", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "ESCALATE", reviewer_id)
                    st.warning("Escalated to Specialist Fraud Prevention Unit.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.button("❓ Request Customer Info", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "REQUEST_INFORMATION", reviewer_id)
                    st.info("Customer notified to provide missing evidence.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

            if st.button("✔️ Mark Resolved", use_container_width=True):
                try:
                    execute_review(ticket["ticket_id"], "RESOLVE", reviewer_id)
                    st.success("Ticket closed and marked RESOLVED.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


# ---------------------------------------------------------------------------
# Interactive Terminal CLI Mode (Fallback if Streamlit is not installed)
# ---------------------------------------------------------------------------

def run_cli_interactive_hitl():
    print("=" * 75)
    print("    HUMAN-IN-THE-LOOP (HITL) SUPERVISOR TERMINAL CONSOLE")
    print("=" * 75)
    
    tickets = get_pending_review_tickets()
    if not tickets:
        print("\nNo tickets currently pending human review! Queue is empty.")
        return

    print(f"\nFound {len(tickets)} tickets requiring supervisor action:\n")
    for i, t in enumerate(tickets, 1):
        print(f"[{i}] {t['ticket_id']} | Priority: {t['priority']} | Status: {t['status']} | SLA: {t['sla_state']} ({t['sla_remaining_minutes']}m left)")
        print(f"    Intent: {t['intent']} | Customer: {t['customer_id']}")
        print(f"    Summary: {t.get('resolution_summary')[:80]}...\n")

    print("-" * 75)
    choice = input("Enter ticket number to review (or 'q' to quit): ").strip()
    if choice.lower() == 'q' or not choice.isdigit():
        return

    idx = int(choice) - 1
    if idx < 0 or idx >= len(tickets):
        print("Invalid selection.")
        return

    selected = tickets[idx]
    print("\n" + "=" * 50)
    print(f"REVIEWING TICKET: {selected['ticket_id']}")
    print(f"Priority: {selected['priority']} | Status: {selected['status']}")
    print(f"Proposed Response: {selected.get('resolution_summary')}")
    print("=" * 50)

    print("\nSelect Action:")
    print("1. APPROVE (Dispatch proposed response)")
    print("2. EDIT (Modify resolution response)")
    print("3. HOLD (Pause processing and SLA clock)")
    print("4. REQUEST_INFORMATION (Ask customer for more info)")
    print("5. ESCALATE (Transfer to Fraud Specialist)")
    print("6. RESOLVE (Confirm outcome and close)")

    action_map = {
        "1": "APPROVE",
        "2": "EDIT",
        "3": "HOLD",
        "4": "REQUEST_INFORMATION",
        "5": "ESCALATE",
        "6": "RESOLVE"
    }

    act_choice = input("Enter choice (1-6): ").strip()
    action = action_map.get(act_choice)
    if not action:
        print("Invalid choice.")
        return

    edited_text = None
    if action == "EDIT":
        edited_text = input("Enter corrected response text: ").strip()

    reviewer_id = input("Enter Reviewer ID (e.g. REV_AGENT_01, REV_FRAUD_OFFICER_03): ").strip() or "REV_AGENT_01"

    try:
        updated = execute_review(selected["ticket_id"], action, reviewer_id, edited_text)
        print(f"\n[SUCCESS] Action '{action}' applied successfully.")
        print(f"New Status: {updated['status']}")
    except Exception as e:
        print(f"\n[ERROR] Action failed: {e}")


if __name__ == "__main__":
    # If launched with `streamlit run hitl_dashboard.py`, streamlit module is in sys.modules
    if "streamlit" in sys.modules or (len(sys.argv) > 1 and sys.argv[1] == "--streamlit"):
        run_streamlit_app()
    else:
        run_cli_interactive_hitl()
