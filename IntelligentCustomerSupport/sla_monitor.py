"""
SLA Monitoring and Tracking Engine
Calculates deadlines from priority SLA targets, monitors elapsed duration,
and identifies AT_RISK or BREACHED tickets according to regulatory requirements.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

DEFAULT_SLA_MINUTES = {
    "P1": 15,
    "P2": 60,
    "P3": 240,
    "P4": 1440
}

def load_sla_config(config_path: str = "config.json") -> Dict[str, Any]:
    file_path = Path(config_path)
    if file_path.exists():
        try:
            with open(file_path, "r", encoding="utf-8") as file:
                cfg = json.load(file)
                return cfg.get("sla_minutes", DEFAULT_SLA_MINUTES)
        except Exception:
            return DEFAULT_SLA_MINUTES
    return DEFAULT_SLA_MINUTES

def calculate_sla_deadline(creation_time_iso: str, priority: str, 
                           sla_minutes_map: Optional[Dict[str, int]] = None) -> str:
    """
    Computes SLA Deadline: TicketCreationTime + ConfiguredSLADuration
    Returns ISO 8601 string.
    """
    if sla_minutes_map is None:
        sla_minutes_map = DEFAULT_SLA_MINUTES
    
    duration = sla_minutes_map.get(priority, 240)
    
    # Parse creation time safely
    try:
        # Handle trailing Z or offsets
        dt = datetime.fromisoformat(creation_time_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc)

    deadline = dt + timedelta(minutes=duration)
    return deadline.isoformat()

def evaluate_sla_status(ticket: Dict[str, Any], current_time: Optional[datetime] = None,
                        at_risk_ratio: float = 0.25) -> Tuple[str, float]:
    """
    Evaluates current SLA compliance state for a ticket.
    Returns: (status, remaining_minutes)
    Status values:
    - PAUSED: If ticket is ON_HOLD or AWAITING_CUSTOMER
    - WITHIN_SLA: Resolution/response completed on time, or deadline is comfortably ahead
    - AT_RISK: Time remaining is <= at_risk_ratio of total allowed duration
    - BREACHED: Deadline has passed while ticket remains active and unresolved
    """
    current_status = ticket.get("status", "OPEN")
    
    # Paused conditions
    if current_status in ("ON_HOLD", "AWAITING_CUSTOMER"):
        return "PAUSED", 0.0

    deadline_str = ticket.get("first_response_due") or ticket.get("resolution_due")
    created_str = ticket.get("created_at")
    
    if not deadline_str or not created_str:
        return "WITHIN_SLA", 999.0

    if current_time is None:
        current_time = datetime.now(timezone.utc)
    elif current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    try:
        deadline_dt = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
    except Exception:
        return "WITHIN_SLA", 999.0

    total_duration_sec = (deadline_dt - created_dt).total_seconds()
    remaining_sec = (deadline_dt - current_time).total_seconds()
    remaining_minutes = remaining_sec / 60.0

    # If ticket was already resolved, check whether resolved before deadline
    if current_status in ("RESOLVED", "CLOSED", "APPROVED_FOR_RESPONSE"):
        updated_str = ticket.get("updated_at", created_str)
        try:
            updated_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated_dt <= deadline_dt:
                return "WITHIN_SLA", remaining_minutes
            else:
                return "BREACHED", remaining_minutes
        except Exception:
            return "WITHIN_SLA", remaining_minutes

    if remaining_sec <= 0:
        return "BREACHED", remaining_minutes
    elif remaining_sec <= (total_duration_sec * at_risk_ratio):
        return "AT_RISK", remaining_minutes
    else:
        return "WITHIN_SLA", remaining_minutes
