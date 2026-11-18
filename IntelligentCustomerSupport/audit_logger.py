"""
Audit Logging Module
Maintains an immutable, compliant audit trail in audit_log.csv.
Sanitizes inputs to prevent sensitive financial data (passwords, CVVs, card numbers) from being stored.
"""

import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Optional

AUDIT_LOG_PATH = Path("audit_log.csv")
AUDIT_FIELDS = [
    "timestamp", "event_id", "query_id", "ticket_id", 
    "event_type", "ticket_status", "details"
]

def sanitize_details(details: str) -> str:
    """Masks potential credit card numbers, CVVs, and sensitive auth data."""
    if not details:
        return ""
    # Mask 16-digit card patterns
    details = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[CARD_MASKED]", details)
    # Mask passwords or tokens
    details = re.sub(r"(?i)(password|token|pin|cvv)\s*[:=]\s*\S+", r"\1=[REDACTED]", details)
    return details

def log_event(query_id: str, ticket_id: str, event_type: str, 
              ticket_status: str, details: str = "", 
              log_path: Path = AUDIT_LOG_PATH) -> dict:
    """Records significant lifecycle, triage, and retry events into the audit trail."""
    now = datetime.now(timezone.utc).isoformat()
    event_id = f"AUD-{uuid4().hex[:8].upper()}"
    sanitized = sanitize_details(str(details))
    
    record = {
        "timestamp": now,
        "event_id": event_id,
        "query_id": query_id or "N/A",
        "ticket_id": ticket_id or "N/A",
        "event_type": event_type,
        "ticket_status": ticket_status,
        "details": sanitized
    }

    exists = log_path.exists()
    with log_path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=AUDIT_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow(record)

    return record
