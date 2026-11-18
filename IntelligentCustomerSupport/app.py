"""
Main Application & End-to-End Orchestrator
Intelligent Banking Dispute & Fraud Triage System with Multi-Tier Escalation
Connects Ingestion, Classification, Knowledge Retrieval, Routing, Ticket Lifecycle,
Notifications, SLA Monitoring, and Compliance Audit Logging.
"""

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Tuple

from classifier import classify_query
from resolver import load_knowledge_base, retrieve_article
from workflow_router import route_query
from ticket_manager import (
    init_db, create_ticket, save_ticket, get_ticket, 
    apply_review_action, reopen_ticket
)
from sla_monitor import calculate_sla_deadline, evaluate_sla_status, load_sla_config
from notifications import generate_notification, send_with_retry
from audit_logger import log_event

REQUIRED_FIELDS = {"query_id", "customer_id", "query_text", "created_at"}

def ingest_queries(file_path: str = "customer_queries.csv") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Ingests customer queries from CSV, applies data validation rules,
    detects duplicates, and separates valid records from rejected ones.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    valid_queries = []
    rejected_queries = []
    seen_ids = set()

    with open(path, mode="r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not REQUIRED_FIELDS.issubset(set(reader.fieldnames)):
            raise ValueError(f"Missing required CSV columns. Required: {REQUIRED_FIELDS}")

        for row in reader:
            cleaned_row = {k: (v or "").strip() for k, v in row.items()}
            errors = []

            # Rule 1: Check required fields
            if not all(cleaned_row.get(f) for f in REQUIRED_FIELDS):
                errors.append("Missing required field")

            query_id = cleaned_row.get("query_id", "")
            
            # Rule 2: Check query_id uniqueness
            if query_id in seen_ids:
                errors.append(f"Duplicate query ID: {query_id}")
            elif query_id:
                seen_ids.add(query_id)

            # Rule 3: Check query_text not empty or whitespace
            if not cleaned_row.get("query_text", "").strip():
                errors.append("Empty query text")

            if errors:
                rejected_queries.append({
                    "record": cleaned_row,
                    "errors": errors
                })
                # Audit log rejection
                log_event(
                    query_id=query_id or "UNKNOWN",
                    ticket_id="N/A",
                    event_type="QUERY_VALIDATION_REJECTED",
                    ticket_status="REJECTED",
                    details=f"Errors: {', '.join(errors)}"
                )
            else:
                valid_queries.append(cleaned_row)

    return valid_queries, rejected_queries

def process_query(query: Dict[str, Any], knowledge_base: List[Dict[str, Any]], 
                  sla_config: Dict[str, int]) -> Dict[str, Any]:
    """
    Orchestrates the lifecycle for a single validated customer query:
    1. Classifies intent, complexity, and priority
    2. Searches approved knowledge base
    3. Evaluates workflow route
    4. Calculates SLA deadlines
    5. Creates and stores persistent ticket
    6. Dispatches notification drafts with retry
    7. Records audit log
    """
    start_time = time.time()
    query_text = query["query_text"]
    query_id = query["query_id"]

    # 1. Classification
    classification = classify_query(query_text)
    priority = classification["priority"]

    # 2. Knowledge Retrieval & Confidence Scoring
    article, match_score = retrieve_article(query_text, knowledge_base)
    article_found = article is not None

    # 3. Workflow Routing
    route = route_query(classification, match_score, article_found)

    # 4. SLA Deadline Calculation
    creation_time = query.get("created_at", datetime.now(timezone.utc).isoformat())
    first_response_due = calculate_sla_deadline(creation_time, priority, sla_config)
    resolution_due = calculate_sla_deadline(creation_time, priority, sla_config)

    # 5. Ticket Creation
    ticket = create_ticket(
        query=query,
        classification=classification,
        first_response_due=first_response_due,
        resolution_due=resolution_due
    )

    # 6. Apply Route Decisions to Ticket State
    if route == "PROPOSE_AUTO_RESOLUTION":
        ticket["status"] = "PENDING_APPROVAL"
        ticket["assigned_team"] = "Auto-Response Queue"
        ticket["resolution_summary"] = article["response"]
    elif route == "URGENT_HUMAN_ESCALATION":
        ticket["status"] = "ESCALATED"
        ticket["assigned_team"] = "Tier-3 Fraud Prevention Unit"
        ticket["resolution_summary"] = "Critical priority fraud/security incident routed for immediate manual triage."
    elif route == "TECHNICAL_HUMAN_REVIEW":
        ticket["status"] = "ESCALATED"
        ticket["assigned_team"] = "Core Banking Engineering"
        ticket["resolution_summary"] = "High complexity technical issue routed for investigation."
    elif route == "CREATE_TICKET_AND_ESCALATE":
        ticket["status"] = "ESCALATED"
        ticket["assigned_team"] = "Specialist Dispute Support"
        ticket["resolution_summary"] = "No approved knowledge base article matched; manual drafting required."
    elif route == "HUMAN_REVIEW":
        ticket["status"] = "IN_REVIEW"
        ticket["assigned_team"] = "Tier-1 Customer Support"
        ticket["resolution_summary"] = article["response"] if article else None
    else:  # REQUEST_INFORMATION_OR_ESCALATE
        ticket["status"] = "AWAITING_CUSTOMER"
        ticket["assigned_team"] = "Customer Care Desk"
        ticket["resolution_summary"] = "Insufficient details provided by customer; clarification requested."

    # Persist in SQLite
    save_ticket(ticket)

    # 7. Audit Logging
    log_event(
        query_id=query_id,
        ticket_id=ticket["ticket_id"],
        event_type="QUERY_PROCESSED_AND_ROUTED",
        ticket_status=ticket["status"],
        details=f"Intent: {classification['intent']}, Priority: {priority}, Route: {route}, Score: {match_score}%"
    )

    # 8. Notifications Dispatch with Retry
    event_type = "ESCALATED" if ticket["status"] == "ESCALATED" else "CREATED"
    notif = generate_notification(ticket, event_type)
    
    # Fast mock sender: always succeeds on first try unless mock error is injected
    delivery_result = send_with_retry(notif, lambda: True, max_attempts=3, backoff_seconds=[0, 0, 0])
    
    elapsed = time.time() - start_time

    return {
        "query_id": query_id,
        "classification": classification,
        "article_id": article["article_id"] if article else None,
        "match_score": match_score,
        "route": route,
        "ticket": ticket,
        "notification_delivery": delivery_result,
        "processing_time_sec": round(elapsed, 4)
    }

def run_evaluation_pipeline(csv_path: str = "customer_queries.csv", kb_path: str = "knowledge_base.json") -> Dict[str, Any]:
    """
    Executes the entire end-to-end laboratory evaluation pipeline and
    computes all mandatory academic metrics as defined in pages 28-30.
    """
    init_db("tickets.db")
    kb = load_knowledge_base(kb_path)
    sla_config = load_sla_config("config.json")

    valid_queries, rejected_queries = ingest_queries(csv_path)
    processed_results = []
    
    total_valid = len(valid_queries)
    total_rejected = len(rejected_queries)
    
    resolved_count = 0
    escalated_count = 0
    sla_met_count = 0

    for query in valid_queries:
        res = process_query(query, kb, sla_config)
        processed_results.append(res)
        
        ticket = res["ticket"]
        # In this workflow, auto-resolved eligible queries are those proposed for auto-resolution
        if res["route"] == "PROPOSE_AUTO_RESOLUTION":
            resolved_count += 1
        elif ticket["status"] in ("ESCALATED", "IN_REVIEW"):
            escalated_count += 1

        # Check SLA status
        sla_status, _ = evaluate_sla_status(ticket)
        if sla_status in ("WITHIN_SLA", "PAUSED"):
            sla_met_count += 1

    resolution_rate = (resolved_count / total_valid * 100) if total_valid > 0 else 0.0
    escalation_rate = (escalated_count / total_valid * 100) if total_valid > 0 else 0.0
    sla_compliance = (sla_met_count / total_valid * 100) if total_valid > 0 else 0.0

    report = {
        "total_queries_ingested": total_valid + total_rejected,
        "valid_queries_count": total_valid,
        "rejected_queries_count": total_rejected,
        "validation_success_rate": round(total_valid / (total_valid + total_rejected) * 100, 2),
        "auto_resolution_rate": round(resolution_rate, 2),
        "escalation_rate": round(escalation_rate, 2),
        "sla_compliance_rate": round(sla_compliance, 2),
        "processed_tickets": [r["ticket"]["ticket_id"] for r in processed_results]
    }
    return report

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--hitl":
        from hitl_dashboard import run_cli_interactive_hitl
        run_cli_interactive_hitl()
    else:
        print("=" * 70)
        print("INTELLIGENT BANKING DISPUTE & FRAUD TRIAGE SYSTEM")
        print("=" * 70)
        report = run_evaluation_pipeline()
        print("\n--- Pipeline Evaluation Report ---")
        print(json.dumps(report, indent=2))
        print("\nBatch execution complete. Check tickets.db, audit_log.csv, and notification_log.csv.")
        print("\nTip: Run 'python app.py --hitl' to launch the interactive Human-in-the-Loop review console!")
