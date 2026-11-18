"""
Notification Delivery & Retry Engine
Generates event notifications, simulates delivery with exponential backoff retries,
and prevents duplicate dispatches using idempotency keys.
"""

import csv
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Dict, Any, Callable, Optional, Set

NOTIFICATION_LOG_PATH = Path("notification_log.csv")
NOTIFICATION_FIELDS = [
    "timestamp", "notification_id", "ticket_id", "event", 
    "recipient", "attempt", "status", "message", "error"
]

def generate_notification(ticket: Dict[str, Any], event: str, recipient: str = "customer@example.com") -> Dict[str, Any]:
    """Generates a structured notification draft with a unique ID and idempotency key."""
    ticket_id = ticket.get("ticket_id", "UNKNOWN")
    notification_id = f"NOTIF-{uuid4().hex[:8].upper()}"
    idempotency_key = f"{ticket_id}:{event}"

    templates = {
        "CREATED": f"Banking Support Ticket {ticket_id} has been created and assigned to triage.",
        "ASSIGNED": f"Ticket {ticket_id} has been assigned to a representative for investigation.",
        "ESCALATED": f"URGENT: Ticket {ticket_id} has been escalated to the Fraud & High-Risk Team.",
        "RESOLVED": f"Ticket {ticket_id} has been resolved. Summary: {ticket.get('resolution_summary', 'Issue addressed')}",
        "SLA_BREACH": f"CRITICAL SLA ALERT: Ticket {ticket_id} has exceeded its response/resolution SLA deadline.",
        "SLA_AT_RISK": f"WARNING: Ticket {ticket_id} is approaching its SLA deadline.",
        "INFO_REQUESTED": f"Action Required: Additional details needed from customer for ticket {ticket_id}."
    }

    message = templates.get(event, f"Ticket {ticket_id} has been updated with event: {event}")

    return {
        "notification_id": notification_id,
        "ticket_id": ticket_id,
        "event": event,
        "recipient": recipient,
        "message": message,
        "status": "DRAFT",
        "idempotency_key": idempotency_key
    }

def log_notification_attempt(record: Dict[str, Any], path: Path = NOTIFICATION_LOG_PATH):
    """Appends delivery attempts to notification_log.csv."""
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=NOTIFICATION_FIELDS)
        if not exists:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "notification_id": record.get("notification_id", ""),
            "ticket_id": record.get("ticket_id", ""),
            "event": record.get("event", ""),
            "recipient": record.get("recipient", ""),
            "attempt": record.get("attempt", 1),
            "status": record.get("status", ""),
            "message": record.get("message", ""),
            "error": record.get("error", "")
        })

# Global idempotency registry in memory
PROCESSED_IDEMPOTENCY_KEYS: Set[str] = set()

def send_with_retry(notification: Dict[str, Any], send_function: Callable[[], bool], 
                    max_attempts: int = 3, backoff_seconds: Optional[list] = None,
                    log_path: Path = NOTIFICATION_LOG_PATH) -> Dict[str, Any]:
    """
    Simulates notification delivery with exponential backoff and idempotency protection.
    """
    idempotency_key = notification.get("idempotency_key")
    if idempotency_key and idempotency_key in PROCESSED_IDEMPOTENCY_KEYS:
        # Prevent duplicate dispatch
        log_notification_attempt({
            **notification,
            "attempt": 0,
            "status": "SKIPPED_DUPLICATE",
            "error": "Duplicate notification event suppressed via idempotency key"
        }, log_path)
        return {
            "status": "SKIPPED_DUPLICATE",
            "attempts": 0,
            "error": "Idempotency key already satisfied"
        }

    if backoff_seconds is None:
        backoff_seconds = [1, 2, 4]

    for attempt in range(1, max_attempts + 1):
        try:
            success = send_function()
            if success:
                if idempotency_key:
                    PROCESSED_IDEMPOTENCY_KEYS.add(idempotency_key)
                notification["status"] = "SENT"
                log_notification_attempt({
                    **notification,
                    "attempt": attempt,
                    "status": "SENT",
                    "error": ""
                }, log_path)
                return {
                    "status": "SENT",
                    "attempts": attempt,
                    "error": None
                }
            raise RuntimeError("Temporary upstream notification provider timeout")
        except Exception as error:
            log_notification_attempt({
                **notification,
                "attempt": attempt,
                "status": "RETRYING" if attempt < max_attempts else "FAILED",
                "error": str(error)
            }, log_path)

            if attempt == max_attempts:
                notification["status"] = "FAILED"
                return {
                    "status": "FAILED",
                    "attempts": attempt,
                    "error": str(error)
                }

            # Wait with exponential backoff
            sleep_duration = backoff_seconds[attempt - 1] if (attempt - 1) < len(backoff_seconds) else (2 ** (attempt - 1))
            time.sleep(sleep_duration)

    return {"status": "FAILED", "attempts": max_attempts, "error": "Max retries exceeded"}
