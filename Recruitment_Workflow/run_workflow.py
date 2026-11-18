"""Command-line runner for the Campus Placement Screening Workflow.

Examples
  python run_workflow.py                      # ingest, score, route, draft notifications (no decisions made)
  python run_workflow.py --review auto        # also simulate a coordinator reviewing (demo policy)
  python run_workflow.py --review interactive # review in the terminal (asks for login)
  python run_workflow.py --reset --review auto --report   # fresh run + lab_report.md
"""
from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from datetime import datetime
from pathlib import Path

import recruitment_workflow as rw

BASE = Path(__file__).resolve().parent
DB, AUDIT, REJECTED, USERS = BASE / "workflow.db", BASE / "audit_log.csv", BASE / "rejected_records.csv", BASE / "users.json"


def reset_state() -> None:
    """Archive (never delete) the previous run's state so history is preserved."""
    existing = [p for p in (DB, AUDIT, REJECTED) if p.exists()]
    if not existing:
        return
    dest = BASE / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    for p in existing:
        shutil.move(str(p), dest / p.name)
    print(f"Previous state archived to {dest.relative_to(BASE)}")


def print_block(app: dict) -> None:
    print("========== PLACEMENT WORKFLOW ==========")
    print(f"Candidate ID: {app['candidate_id']}")
    print("Validation: VALID")
    print(f"Score: {app['score']:g}")
    print(f"Proposed Status: {app['proposed_status']}")
    print(f"Notification: {app['notif_state']}")
    print(f"Human Review: {'PENDING' if not app['review_action'] else app['review_action']}")
    print("-" * 42)


def auto_review(coordinator: dict) -> None:
    """Deterministic demo policy standing in for a human coordinator (for the lab observation table)."""
    apps = rw.list_applications(DB)
    manual_promoted = 0
    for i, a in enumerate(apps):
        cid, st, flags = a["candidate_id"], a["proposed_status"], rw.json.loads(a["flags"] or "[]")
        try:
            if st == rw.STATUS_SHORTLIST and not flags:
                rw.apply_review(DB, AUDIT, cid, coordinator, "APPROVE", "Meets shortlist criteria", users_path=USERS)
            elif st == rw.STATUS_MANUAL and flags and "project" in " ".join(flags).lower():
                rw.apply_review(DB, AUDIT, cid, coordinator, "REQUEST_INFO",
                                "Ask student to submit project/internship details", users_path=USERS)
            elif st == rw.STATUS_MANUAL and not flags and a["score"] >= 70 and manual_promoted < 3:
                manual_promoted += 1
                rw.apply_review(DB, AUDIT, cid, coordinator, "EDIT",
                                "Strong profile after manual CV check; promoted to shortlist review",
                                new_status=rw.STATUS_SHORTLIST, users_path=USERS)
            elif st == rw.STATUS_MANUAL and i % 3 == 0:
                rw.apply_review(DB, AUDIT, cid, coordinator, "HOLD", "Waiting for department verification", users_path=USERS)
            elif st == rw.STATUS_NOT_SHORTLISTED and not flags and a["score"] >= 45:
                rw.apply_review(DB, AUDIT, cid, coordinator, "APPROVE", "Reviewed; neutral status update approved", users_path=USERS)
        except rw.WorkflowError as exc:
            print(f"  review skipped for {cid}: {exc}")


def interactive_review() -> None:
    username = input("Reviewer username: ").strip()
    user = rw.authenticate(username, getpass.getpass("Password: "), USERS)
    if not user:
        rw.AuditLog(AUDIT).append("LOGIN_FAILED", reviewer_id=username, error_details="Invalid credentials")
        print("Login failed.")
        return
    print(f"Logged in as {user['user_id']} ({user['role']}). Actions: APPROVE / EDIT / HOLD / REQUEST_INFO / SKIP / QUIT")
    for app in rw.list_applications(DB):
        if app["review_action"] and app["final_status"] in rw.ROUTING_STATUSES:
            continue
        print()
        print(rw.explain_score(app["candidate_id"], {"score": app["score"], "rule_version": app["rule_version"],
                                                       "breakdown": rw.json.loads(app["score_breakdown"])}))
        print(f"Proposed status: {app['proposed_status']}")
        for f in rw.json.loads(app["flags"] or "[]"):
            print("  flag:", f)
        action = input("Action: ").strip().upper()
        if action == "QUIT":
            break
        if action in ("", "SKIP"):
            continue
        notes = input("Notes (required for EDIT / REQUEST_INFO): ")
        new_status = None
        if action == "EDIT":
            new_status = input(f"New status ({'/'.join(rw.ROUTING_STATUSES)}, blank to keep): ").strip().upper() or None
        try:
            res = rw.apply_review(DB, AUDIT, app["candidate_id"], user, action, notes, new_status=new_status, users_path=USERS)
            print("  ->", res["final_status"], "| notification:", res["notification_status"])
        except (rw.WorkflowError, ValueError) as exc:
            print("  !", exc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates", default=str(BASE / "candidates.csv"))
    ap.add_argument("--rules", default=str(BASE / "rules.json"))
    ap.add_argument("--review", choices=["none", "auto", "interactive"], default="none")
    ap.add_argument("--reset", action="store_true", help="archive previous state and start fresh")
    ap.add_argument("--report", action="store_true", help="run tests and write lab_report.md")
    ap.add_argument("--show", type=int, default=5, help="how many candidate blocks to print")
    args = ap.parse_args()

    if args.reset:
        reset_state()
    rw.ensure_default_users(USERS)

    try:
        summary = rw.run_pipeline(args.candidates, args.rules, DB, AUDIT, REJECTED, actor="cli", verbose=True)
    except rw.WorkflowError as exc:
        print(f"ERROR: {exc}\nScoring was not started.")
        return 1

    for app in rw.list_applications(DB)[: args.show]:
        print_block(app)
    print("Invalid Records:", summary["invalid_records"], f"(duplicates: {summary['duplicates_detected']})")
    print("Candidates Processed:", summary["processed"], f"(skipped as already processed: {summary['skipped_already_processed']})")

    if args.review == "auto":
        coordinator = rw.authenticate("coordinator1", rw.DEFAULT_USERS["coordinator1"][0], USERS)
        # demonstrate access control: a viewer attempts to approve and is denied + logged
        viewer = rw.authenticate("viewer", rw.DEFAULT_USERS["viewer"][0], USERS)
        first = next(a for a in rw.list_applications(DB) if not a["review_action"])
        try:
            rw.apply_review(DB, AUDIT, first["candidate_id"], viewer, "APPROVE", users_path=USERS)
        except rw.PermissionDenied as exc:
            print("Access control check:", exc)
        auto_review(coordinator)
    elif args.review == "interactive":
        interactive_review()

    rc = rw.review_counts(DB)
    print("Human Reviews Pending:", rc["pending"], "| Completed:", rc["completed"])
    ok, n, msg = rw.AuditLog(AUDIT).verify()
    print(f"Audit Log: Generated ({n} events) - {msg}")

    if args.report:
        import generate_report
        path = generate_report.build_report(summary, args.rules)
        print("Report written:", path.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
