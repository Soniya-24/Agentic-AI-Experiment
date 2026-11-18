"""Notification drafting and (simulated) dispatch for the Campus Placement Screening Workflow.

Nothing here talks to a real mail server. Every message starts life as a DRAFT and is only
"sent" (simulated) after an authorised reviewer approves it in recruitment_workflow.apply_review().
"""
from __future__ import annotations

import re
from typing import Callable

DRAFT = "DRAFT"
SENT = "SENT"
FAILED = "FAILED"
NOT_SENT = "NOT_SENT"

COMPANY_NAME = "the visiting company"

_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# Words an approved message must never contain (no score leakage, no premature rejection).
_FORBIDDEN_ALWAYS = ("score", "cgpa", "percentile", "rank ")
_FORBIDDEN_FOR_NOT_SHORTLISTED = ("rejected", "reject", "not selected", "unsuccessful", "regret to inform")


class DeliveryError(Exception):
    """Raised by a sender when a message could not be delivered."""


def _safe_name(name: str) -> str:
    return re.sub(r"\s+", " ", _CTRL.sub(" ", str(name or ""))).strip() or "Student"


def generate_notification(candidate: dict, status: str, company: str = COMPANY_NAME) -> dict:
    """Return a DRAFT notification for the proposed workflow status.

    Drafts never include scores or any other student's information, and the
    NOT_SHORTLISTED_REVIEW draft never states that a final rejection has occurred.
    """
    name = _safe_name(candidate.get("candidate_name"))
    candidate_id = str(candidate.get("candidate_id", "")).strip()
    ref = f"Application reference: {candidate_id}"

    if status == "SHORTLIST_REVIEW":
        subject = f"Placement Application Update [{candidate_id}]"
        message = (
            f"Dear {name},\n\n"
            f"Your application for {company} is being considered for the next stage of the "
            "campus placement process. The Placement Cell will review your application and "
            "communicate the next steps, including any test or interview schedule.\n\n"
            f"{ref}\n\nRegards,\nTraining & Placement Cell"
        )
    elif status == "MANUAL_REVIEW":
        subject = f"Placement Application Under Review [{candidate_id}]"
        message = (
            f"Dear {name},\n\n"
            f"Your application for {company} is currently under review by the Placement Cell. "
            "We may contact you if any additional information is needed and will share further "
            "updates as soon as they are available.\n\n"
            f"{ref}\n\nRegards,\nTraining & Placement Cell"
        )
    else:  # NOT_SHORTLISTED_REVIEW and anything unexpected: stay neutral
        subject = f"Placement Application Review Update [{candidate_id}]"
        message = (
            f"Dear {name},\n\n"
            f"Your application for {company} is undergoing a review by the Placement Cell. "
            "You will be informed of the outcome once the review is complete.\n\n"
            f"{ref}\n\nRegards,\nTraining & Placement Cell"
        )

    return {
        "candidate_id": candidate_id,
        "subject": subject,
        "message": message,
        "draft_status": DRAFT,
        "proposed_status": status,
    }


def validate_message(message: str, status: str) -> str:
    """Check a reviewer-edited message. Returns the cleaned message or raises ValueError."""
    text = str(message or "").replace("\r\n", "\n").strip()
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if len(text) < 20:
        raise ValueError("Message is too short to send")
    if len(text) > 2000:
        raise ValueError("Message exceeds 2000 characters")
    lowered = text.lower()
    for word in _FORBIDDEN_ALWAYS:
        if word in lowered:
            raise ValueError(f"Message must not reveal internal evaluation details (found '{word.strip()}')")
    if status == "NOT_SHORTLISTED_REVIEW":
        for word in _FORBIDDEN_FOR_NOT_SHORTLISTED:
            if word in lowered:
                raise ValueError("Message must not imply a final rejection while the application is under review")
    return text


def simulated_sender(draft: dict, to_email: str) -> None:
    """Stand-in for an SMTP client. Addresses on @bounce.example always fail (to test error handling)."""
    if str(to_email).lower().endswith("@bounce.example"):
        raise DeliveryError(f"Simulated SMTP failure: mailbox unavailable for {to_email}")
    # A real implementation would call smtplib here. The lab never does.
    return None


def dispatch_with_retry(
    draft: dict,
    to_email: str,
    sender: Callable[[dict, str], None] | None = None,
    max_retries: int = 2,
) -> tuple[str, int, str]:
    """Try to send a draft, retrying up to ``max_retries`` times.

    Returns (state, attempts, error) where state is SENT or FAILED. Never raises for delivery problems:
    a failed notification must not lose the application record.
    """
    sender = sender or simulated_sender
    attempts = 0
    last_error = ""
    for _ in range(max_retries + 1):
        attempts += 1
        try:
            sender(draft, to_email)
            return SENT, attempts, ""
        except Exception as exc:  # delivery layer can fail in any way
            last_error = f"{type(exc).__name__}: {exc}"
    return FAILED, attempts, last_error
