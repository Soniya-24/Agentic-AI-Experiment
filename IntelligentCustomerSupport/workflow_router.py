"""
Workflow Routing Engine
Directs queries to auto-resolution, human review, or immediate high-priority escalation.
Enforces safety rules: P1/Critical security issues bypass automated answers completely.
"""

def route_query(classification: dict, match_score: float, article_found: bool) -> str:
    """
    Evaluates classification attributes and knowledge match score to determine route.
    Possible Routes:
    - URGENT_HUMAN_ESCALATION: P1 critical fraud, outage, or security breach.
    - TECHNICAL_HUMAN_REVIEW: High complexity query requiring engineering or fraud analyst review.
    - CREATE_TICKET_AND_ESCALATE: No approved knowledge base article available.
    - PROPOSE_AUTO_RESOLUTION: Safe, low-risk query with high confidence (match_score >= 70).
    - HUMAN_REVIEW: Medium confidence (40 <= match_score < 70) requiring agent sign-off.
    - REQUEST_INFORMATION_OR_ESCALATE: Ambiguous query or low match score (< 40).
    """
    priority = classification.get("priority", "P3")
    complexity = classification.get("complexity", "Low")
    intent = classification.get("intent", "")

    # Rule 1: P1 Critical or Security Incident ALWAYS escalates immediately regardless of score
    if priority == "P1" or intent in ("Security / Fraud", "Technical Error" if priority == "P1" else ""):
        return "URGENT_HUMAN_ESCALATION"

    # Rule 2: High complexity queries require human intervention
    if complexity == "High":
        return "TECHNICAL_HUMAN_REVIEW"

    # Rule 3: If no approved article is found, never invent an answer
    if not article_found:
        return "CREATE_TICKET_AND_ESCALATE"

    # Rule 4: High match score on low complexity, non-critical query -> Propose auto-resolution
    if match_score >= 70.0 and complexity == "Low" and priority in ("P3", "P4"):
        return "PROPOSE_AUTO_RESOLUTION"

    # Rule 5: Moderate match score -> Propose draft for Human Review
    if match_score >= 40.0:
        return "HUMAN_REVIEW"

    # Rule 6: Low match score (< 40%) or ungrounded query
    return "REQUEST_INFORMATION_OR_ESCALATE"
