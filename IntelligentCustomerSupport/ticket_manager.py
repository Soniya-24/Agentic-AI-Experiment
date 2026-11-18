"""
Ticket Manager & Lifecycle Management Module
Manages ticket creation, state transitions, human review actions, and SQLite persistence.
Enforces authorization and lifecycle validation (e.g. preventing silent reopening).
"""

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4
from typing import Dict, Any, Optional, List

ALLOWED_ACTIONS = {
    "APPROVE", "EDIT", "HOLD", 
    "REQUEST_INFORMATION", "ESCALATE", "RESOLVE"
}

VALID_STATUSES = {
    "OPEN", "TRIAGED", "ASSIGNED", "IN_PROGRESS", 
    "PENDING_APPROVAL", "APPROVED_FOR_RESPONSE", "IN_REVIEW", 
    "ON_HOLD", "AWAITING_CUSTOMER", "ESCALATED", "RESOLVED", "CLOSED"
}

def get_db_connection(db_path: str = "tickets.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = "tickets.db"):
    """Initializes the SQLite schema for ticket persistence."""
    with get_db_connection(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                query_id TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                intent TEXT,
                complexity TEXT,
                priority TEXT,
                assigned_team TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                first_response_due TEXT,
                resolution_due TEXT,
                resolution_summary TEXT,
                last_reviewer TEXT,
                review_action TEXT,
                previous_response TEXT
            )
        """)
        conn.commit()

def create_ticket(query: Dict[str, Any], classification: Dict[str, Any], 
                  first_response_due: Optional[str] = None, 
                  resolution_due: Optional[str] = None) -> Dict[str, Any]:
    """Creates a new ticket dictionary initialized to OPEN state."""
    now = datetime.now(timezone.utc).isoformat()
    ticket_id = "TKT-" + uuid4().hex[:8].upper()
    
    ticket = {
        "ticket_id": ticket_id,
        "query_id": query.get("query_id", ""),
        "customer_id": query.get("customer_id", ""),
        "intent": classification.get("intent", "General Information"),
        "complexity": classification.get("complexity", "Low"),
        "priority": classification.get("priority", "P3"),
        "status": "OPEN",
        "created_at": now,
        "updated_at": now,
        "assigned_team": None,
        "first_response_due": first_response_due,
        "resolution_due": resolution_due,
        "resolution_summary": None,
        "last_reviewer": None,
        "review_action": None,
        "previous_response": None
    }
    return ticket

def save_ticket(ticket: Dict[str, Any], db_path: str = "tickets.db"):
    """Persists or updates a ticket in SQLite."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute("""
            INSERT INTO tickets (
                ticket_id, query_id, customer_id, intent, complexity, priority,
                assigned_team, status, created_at, updated_at, first_response_due,
                resolution_due, resolution_summary, last_reviewer, review_action, previous_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket_id) DO UPDATE SET
                status = excluded.status,
                assigned_team = excluded.assigned_team,
                updated_at = excluded.updated_at,
                resolution_summary = excluded.resolution_summary,
                last_reviewer = excluded.last_reviewer,
                review_action = excluded.review_action,
                previous_response = excluded.previous_response
        """, (
            ticket["ticket_id"], ticket["query_id"], ticket["customer_id"],
            ticket["intent"], ticket["complexity"], ticket["priority"],
            ticket["assigned_team"], ticket["status"], ticket["created_at"],
            ticket["updated_at"], ticket.get("first_response_due"),
            ticket.get("resolution_due"), ticket.get("resolution_summary"),
            ticket.get("last_reviewer"), ticket.get("review_action"),
            ticket.get("previous_response")
        ))
        conn.commit()

def get_ticket(ticket_id: str, db_path: str = "tickets.db") -> Optional[Dict[str, Any]]:
    """Fetches a ticket from the database by ID."""
    init_db(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row:
            return dict(row)
        return None

def apply_review_action(ticket: Dict[str, Any], action: str, reviewer_id: str, 
                        authorized_reviewers: Optional[List[str]] = None,
                        edited_response: Optional[str] = None) -> Dict[str, Any]:
    """
    Applies a human review action with security authorization and state machine transitions.
    Raises PermissionError if reviewer is unauthorized.
    Raises ValueError for invalid actions or invalid transitions.
    """
    # Authorization verification
    if authorized_reviewers is not None and reviewer_id not in authorized_reviewers:
        raise PermissionError(f"Reviewer '{reviewer_id}' is not authorized to perform actions.")

    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"Invalid review action: '{action}'. Must be one of {ALLOWED_ACTIONS}")

    # Guard: Closed ticket cannot be silently modified without reopening
    if ticket.get("status") == "CLOSED" and action != "REOPEN":
        raise ValueError("Cannot perform review actions on a CLOSED ticket. Reopen first.")

    now = datetime.now(timezone.utc).isoformat()
    ticket["last_reviewer"] = reviewer_id
    ticket["review_action"] = action
    ticket["updated_at"] = now

    if action == "APPROVE":
        ticket["status"] = "APPROVED_FOR_RESPONSE"
    elif action == "EDIT":
        ticket["status"] = "IN_REVIEW"
        if edited_response:
            ticket["previous_response"] = ticket.get("resolution_summary")
            ticket["resolution_summary"] = edited_response
    elif action == "HOLD":
        ticket["status"] = "ON_HOLD"
    elif action == "REQUEST_INFORMATION":
        ticket["status"] = "AWAITING_CUSTOMER"
    elif action == "ESCALATE":
        ticket["status"] = "ESCALATED"
    elif action == "RESOLVE":
        ticket["status"] = "RESOLVED"

    return ticket

def reopen_ticket(ticket: Dict[str, Any], reason: str = "Customer reported unresolved issue") -> Dict[str, Any]:
    """Reopens a resolved or closed ticket into active workflow."""
    now = datetime.now(timezone.utc).isoformat()
    ticket["status"] = "OPEN"
    ticket["updated_at"] = now
    ticket["review_action"] = "REOPEN"
    ticket["resolution_summary"] = f"Reopened: {reason}"
    return ticket
