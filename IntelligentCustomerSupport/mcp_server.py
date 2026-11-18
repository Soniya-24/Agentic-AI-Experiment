"""
Model Context Protocol (MCP) Server
Domain: Intelligent Banking Dispute & Fraud Triage System

Exposes standard MCP Tools and Resources so that AI agents (Antigravity, Claude Desktop,
Cursor, or Gemini) can interact with banking tickets, invoke human-in-the-loop workflows,
and query SLA metrics via the standard MCP protocol.
"""

import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

# Local module imports
from classifier import classify_query
from resolver import load_knowledge_base, retrieve_article
from workflow_router import route_query
from ticket_manager import (
    init_db, create_ticket, save_ticket, get_ticket, 
    apply_review_action, get_db_connection
)
from sla_monitor import calculate_sla_deadline, evaluate_sla_status, load_sla_config
from notifications import generate_notification, send_with_retry
from audit_logger import log_event

logger = logging.getLogger("mcp_server")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Ensure database is initialized
init_db("tickets.db")


# ---------------------------------------------------------------------------
# MCP Business Logic Core
# ---------------------------------------------------------------------------

def mcp_triage_query(customer_id: str, query_text: str, product: str = "CoreBanking") -> Dict[str, Any]:
    """
    Ingests and triages a customer banking query.
    Performs classification, knowledge base retrieval, and initial routing.
    If high risk or fraud is detected, creates an escalated ticket.
    """
    kb = load_knowledge_base("knowledge_base.json")
    sla_cfg = load_sla_config("config.json")
    
    classification = classify_query(query_text)
    article, match_score = retrieve_article(query_text, kb)
    route = route_query(classification, match_score, article is not None)
    
    creation_time = datetime.now(timezone.utc).isoformat()
    priority = classification["priority"]
    deadline = calculate_sla_deadline(creation_time, priority, sla_cfg)
    
    mock_query = {
        "query_id": f"MCP-Q-{datetime.now().strftime('%M%S%f')[:8]}",
        "customer_id": customer_id,
        "query_text": query_text,
        "product": product,
        "created_at": creation_time
    }
    
    ticket = create_ticket(mock_query, classification, first_response_due=deadline, resolution_due=deadline)
    
    if route == "PROPOSE_AUTO_RESOLUTION":
        ticket["status"] = "PENDING_APPROVAL"
        ticket["assigned_team"] = "Auto-Response Queue"
        ticket["resolution_summary"] = article["response"] if article else None
    elif route in ("URGENT_HUMAN_ESCALATION", "TECHNICAL_HUMAN_REVIEW", "CREATE_TICKET_AND_ESCALATE", "HUMAN_REVIEW"):
        ticket["status"] = "ESCALATED"
        ticket["assigned_team"] = "Fraud & High-Risk Unit" if priority == "P1" else "Disputes Team"
        ticket["resolution_summary"] = article["response"] if article else "Awaiting human review"
    else:
        ticket["status"] = "AWAITING_CUSTOMER"
        ticket["assigned_team"] = "Customer Care"

    save_ticket(ticket)
    
    # Audit log
    log_event(
        query_id=mock_query["query_id"],
        ticket_id=ticket["ticket_id"],
        event_type="MCP_QUERY_TRIAGED",
        ticket_status=ticket["status"],
        details=f"Route: {route}, Intent: {classification['intent']}, Priority: {priority}"
    )

    return {
        "status": "SUCCESS",
        "ticket_id": ticket["ticket_id"],
        "route": route,
        "classification": classification,
        "match_score": match_score,
        "resolution_summary": ticket["resolution_summary"],
        "assigned_team": ticket["assigned_team"],
        "sla_deadline": deadline
    }

def mcp_get_ticket_details(ticket_id: str) -> Dict[str, Any]:
    """Retrieves full details of a specific ticket including its live SLA status."""
    ticket = get_ticket(ticket_id)
    if not ticket:
        return {"status": "ERROR", "message": f"Ticket {ticket_id} not found."}
    
    sla_status, remaining_minutes = evaluate_sla_status(ticket)
    return {
        "status": "SUCCESS",
        "ticket": ticket,
        "sla_evaluation": {
            "status": sla_status,
            "remaining_minutes": round(remaining_minutes, 2)
        }
    }

def mcp_list_human_review_queue() -> List[Dict[str, Any]]:
    """Lists all tickets requiring Human-in-the-Loop review or supervisor authorization."""
    with get_db_connection("tickets.db") as conn:
        rows = conn.execute("""
            SELECT ticket_id, query_id, customer_id, intent, complexity, priority, 
                   status, assigned_team, first_response_due, resolution_summary 
            FROM tickets 
            WHERE status IN ('PENDING_APPROVAL', 'IN_REVIEW', 'ESCALATED', 'AWAITING_CUSTOMER', 'OPEN')
            ORDER BY 
                CASE priority 
                    WHEN 'P1' THEN 1 
                    WHEN 'P2' THEN 2 
                    WHEN 'P3' THEN 3 
                    ELSE 4 
                END, created_at ASC
        """).fetchall()
        
        results = []
        for r in rows:
            d = dict(r)
            sla_state, mins = evaluate_sla_status(d)
            d["sla_status"] = sla_state
            d["sla_remaining_minutes"] = round(mins, 1)
            results.append(d)
        return results

def mcp_execute_human_review(ticket_id: str, action: str, reviewer_id: str, 
                             edited_response: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes a Human-in-the-Loop (HITL) supervisor action.
    Allowed Actions: APPROVE, EDIT, HOLD, REQUEST_INFORMATION, ESCALATE, RESOLVE.
    Enforces authorized reviewer check and audit logging.
    """
    cfg_path = Path("config.json")
    authorized_reviewers = None
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            authorized_reviewers = cfg.get("authorized_reviewers")

    ticket = get_ticket(ticket_id)
    if not ticket:
        return {"status": "ERROR", "message": f"Ticket {ticket_id} not found."}

    try:
        updated_ticket = apply_review_action(
            ticket=ticket,
            action=action,
            reviewer_id=reviewer_id,
            authorized_reviewers=authorized_reviewers,
            edited_response=edited_response
        )
        save_ticket(updated_ticket)

        # Audit log the HITL decision
        log_event(
            query_id=updated_ticket.get("query_id", "N/A"),
            ticket_id=ticket_id,
            event_type=f"HITL_{action}_PERFORMED",
            ticket_status=updated_ticket["status"],
            details=f"Reviewer: {reviewer_id}. Action: {action}."
        )

        # Send notification upon resolution or escalation
        if action in ("APPROVE", "RESOLVE"):
            notif = generate_notification(updated_ticket, "RESOLVED")
            send_with_retry(notif, lambda: True, max_attempts=3, backoff_seconds=[0, 0, 0])
        elif action == "ESCALATE":
            notif = generate_notification(updated_ticket, "ESCALATED")
            send_with_retry(notif, lambda: True, max_attempts=3, backoff_seconds=[0, 0, 0])

        return {
            "status": "SUCCESS",
            "message": f"Action '{action}' successfully applied by {reviewer_id}.",
            "new_status": updated_ticket["status"],
            "resolution_summary": updated_ticket.get("resolution_summary")
        }
    except Exception as err:
        log_event(
            query_id=ticket.get("query_id", "N/A"),
            ticket_id=ticket_id,
            event_type="HITL_ACTION_FAILED",
            ticket_status=ticket.get("status", "UNKNOWN"),
            details=f"Unauthorized/Invalid attempt by {reviewer_id}: {str(err)}"
        )
        return {"status": "ERROR", "message": str(err)}


# ---------------------------------------------------------------------------
# MCP Server Wrapper (FastMCP if available, else JSON-RPC 2.0 stdio engine)
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP
    mcp_app = FastMCP("BankingDisputeTriageServer")

    @mcp_app.tool()
    def triage_customer_query(customer_id: str, query_text: str, product: str = "CoreBanking") -> str:
        """Triage an incoming banking query with intent classification, heuristic KB retrieval, and workflow routing."""
        res = mcp_triage_query(customer_id, query_text, product)
        return json.dumps(res, indent=2)

    @mcp_app.tool()
    def get_ticket_info(ticket_id: str) -> str:
        """Fetch ticket information, current state, and SLA countdown."""
        res = mcp_get_ticket_details(ticket_id)
        return json.dumps(res, indent=2)

    @mcp_app.tool()
    def list_pending_human_reviews() -> str:
        """List tickets currently queued for Human-in-the-Loop review and supervisor approval."""
        res = mcp_list_human_review_queue()
        return json.dumps(res, indent=2)

    @mcp_app.tool()
    def human_review_action(ticket_id: str, action: str, reviewer_id: str, edited_response: str = "") -> str:
        """Perform a human supervisor action: APPROVE, EDIT, HOLD, REQUEST_INFORMATION, ESCALATE, RESOLVE."""
        res = mcp_execute_human_review(ticket_id, action, reviewer_id, edited_response or None)
        return json.dumps(res, indent=2)

    @mcp_app.resource("banking://knowledge-base")
    def get_knowledge_base_resource() -> str:
        """Approved bank policy knowledge base articles."""
        kb = load_knowledge_base("knowledge_base.json")
        return json.dumps(kb, indent=2)

    @mcp_app.resource("banking://audit-log")
    def get_audit_log_resource() -> str:
        """Regulatory audit log containing compliance event trail."""
        log_file = Path("audit_log.csv")
        return log_file.read_text(encoding="utf-8") if log_file.exists() else "No logs"

    def run_server():
        mcp_app.run()

except ImportError:
    # Lightweight Fallback JSON-RPC 2.0 Server when `mcp` package is not yet installed
    def handle_json_rpc(line: str) -> Optional[dict]:
        try:
            req = json.loads(line)
        except Exception:
            return None

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {
                        "name": "BankingDisputeTriageServer",
                        "version": "1.0.0"
                    },
                    "capabilities": {"tools": {}, "resources": {}}
                }
            }
        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": [
                        {
                            "name": "triage_customer_query",
                            "description": "Triage an incoming banking query with intent classification, heuristic KB retrieval, and routing.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "customer_id": {"type": "string"},
                                    "query_text": {"type": "string"},
                                    "product": {"type": "string", "default": "CoreBanking"}
                                },
                                "required": ["customer_id", "query_text"]
                            }
                        },
                        {
                            "name": "get_ticket_info",
                            "description": "Fetch ticket information, current state, and SLA countdown.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"ticket_id": {"type": "string"}},
                                "required": ["ticket_id"]
                            }
                        },
                        {
                            "name": "list_pending_human_reviews",
                            "description": "List tickets currently queued for Human-in-the-Loop review and supervisor approval.",
                            "inputSchema": {"type": "object", "properties": {}}
                        },
                        {
                            "name": "human_review_action",
                            "description": "Perform a human supervisor action: APPROVE, EDIT, HOLD, REQUEST_INFORMATION, ESCALATE, RESOLVE.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "ticket_id": {"type": "string"},
                                    "action": {"type": "string", "enum": ["APPROVE", "EDIT", "HOLD", "REQUEST_INFORMATION", "ESCALATE", "RESOLVE"]},
                                    "reviewer_id": {"type": "string"},
                                    "edited_response": {"type": "string"}
                                },
                                "required": ["ticket_id", "action", "reviewer_id"]
                            }
                        }
                    ]
                }
            }
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "triage_customer_query":
                res = mcp_triage_query(args.get("customer_id", "GUEST"), args.get("query_text", ""), args.get("product", "CoreBanking"))
            elif name == "get_ticket_info":
                res = mcp_get_ticket_details(args.get("ticket_id", ""))
            elif name == "list_pending_human_reviews":
                res = mcp_list_human_review_queue()
            elif name == "human_review_action":
                res = mcp_execute_human_review(args.get("ticket_id"), args.get("action"), args.get("reviewer_id"), args.get("edited_response"))
            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method {name} not found"}}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}
            }
        elif method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "resources": [
                        {"uri": "banking://knowledge-base", "name": "Approved Knowledge Base"},
                        {"uri": "banking://audit-log", "name": "Regulatory Audit Log"}
                    ]
                }
            }
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    def run_server():
        logger.info("Starting lightweight JSON-RPC stdio MCP server for Banking Dispute Triage...")
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            response = handle_json_rpc(line)
            if response:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

if __name__ == "__main__":
    run_server()
