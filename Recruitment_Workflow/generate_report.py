"""Build lab_report.md: observation table (Step 13), evaluation metrics, fairness and rule-comparison notes."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import recruitment_workflow as rw

BASE = Path(__file__).resolve().parent
DB, AUDIT = BASE / "workflow.db", BASE / "audit_log.csv"


def run_tests() -> dict:
    """Run the pytest suite in a subprocess and return pass/fail counts."""
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
                           str(BASE / "test_recruitment.py")], capture_output=True, text=True, cwd=BASE)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    passed = int((re.search(r"(\d+) passed", tail) or [0, 0])[1])
    failed = int((re.search(r"(\d+) failed", tail) or [0, 0])[1])
    errors = int((re.search(r"(\d+) error", tail) or [0, 0])[1])
    return {"passed": passed, "total": passed + failed + errors, "summary_line": tail}


def _fmt(v, suffix=""):
    return "N/A" if v is None else f"{v}{suffix}"


def build_report(summary: dict, rules_path) -> Path:
    tests = run_tests()
    m = rw.compute_metrics(summary, DB, tests)
    rc = rw.review_counts(DB)
    sc = rw.status_counts(DB)
    ok, n_events, chain_msg = rw.AuditLog(AUDIT).verify()

    lines = [
        "# Lab 7 - Campus Placement Screening Workflow: Execution Report",
        f"_Generated {datetime.now():%Y-%m-%d %H:%M}_ | rules `{summary['rules_file']}` v{summary['rule_version']} | run `{summary['run_id']}`",
        "",
        "## Observations (Step 13)",
        "| Parameter | Observation |", "|---|---|",
        f"| Total candidate records | {summary['total_records']} |",
        f"| Valid records | {summary['valid_records']} |",
        f"| Invalid or quarantined records | {summary['invalid_records']} |",
        f"| Duplicate records detected | {summary['duplicates_detected']} |",
        f"| Candidates in shortlist review | {sc[rw.STATUS_SHORTLIST]} |",
        f"| Candidates in manual review | {sc[rw.STATUS_MANUAL]} |",
        f"| Candidates in not-shortlisted review | {sc[rw.STATUS_NOT_SHORTLISTED]} |",
        f"| Notification drafts generated | {summary['drafts_generated']} |",
        f"| Human approvals / holds / edits / info requests | {rc['approved']} / {rc['held']} / {rc['edited']} / {rc['info_requested']} |",
        f"| Reviewer overrides (status changed) | {rc['overrides']} |",
        f"| Notifications sent / failed / not sent | {rc['notifications_sent']} / {rc['notifications_failed']} / {rc['notifications_not_sent']} |",
        f"| Total execution time | {summary['elapsed_seconds']} s |",
        f"| Test cases passed | {tests['passed']} / {tests['total']} |",
        "",
        "## Evaluation metrics",
        "| Metric | Formula | Value |", "|---|---|---|",
        f"| Data validation rate | valid / total x 100 | {_fmt(m['data_validation_rate_pct'], '%')} |",
        f"| Workflow processing success rate | successfully processed valid / valid x 100 | {_fmt(m['workflow_success_rate_pct'], '%')} |",
        f"| Notification draft generation rate | drafts generated / drafts required x 100 | {_fmt(m['draft_generation_rate_pct'], '%')} |",
        f"| Test pass rate | passed / total x 100 | {_fmt(m['test_pass_rate_pct'], '%')} |",
        f"| Average processing time | execution time / valid records processed | {_fmt(m['avg_processing_ms_per_valid_record'], ' ms')} per record |",
        f"| Human review completion rate | reviewed / requiring review x 100 | {_fmt(m['human_review_completion_pct'], '%')} |",
        f"| Notification delivery success rate | sent / (sent + failed) x 100 | {_fmt(m['notification_delivery_success_pct'], '%')} |",
        "",
        f"Audit log: {n_events} events, {chain_msg.lower()}. Test run: `{tests['summary_line']}`.",
        "",
        "## Fairness-oriented review (synthetic data)",
        "| Branch | Applications | Shortlist review | Rate | Ratio to best | Below 4/5 rule |", "|---|---|---|---|---|---|",
    ]
    for r in rw.fairness_by_branch(DB):
        lines.append(f"| {r['branch']} | {r['applications']} | {r['shortlist_review']} | {r['shortlist_rate_pct']}% | "
                     f"{_fmt(r['ratio_to_best'])} | {'YES' if r['below_four_fifths'] else 'no'} |")
    lines += [
        "",
        "Branches outside the listed eligible branches (e.g. MECH, CIVIL) are routed to MANUAL_REVIEW by design, so their "
        "shortlist-review rate is zero until a human decides. This is a configuration choice that a reviewer should justify "
        "against the role requirements, not a validated fairness result. No gender, caste, religion, age, address or name "
        "field is used in scoring. These metrics measure software behaviour only; they do not prove that the criteria are fair.",
    ]

    try:
        a, b = rw.load_rules(rules_path), rw.load_rules(BASE / "rules_v2.json")
        cmp_rows = rw.compare_rule_versions(DB, a, b)
        changed = [r for r in cmp_rows if r["status_changed"]]
        lines += ["", f"## Rule version comparison (v{a['rule_version']} vs v{b['rule_version']})",
                  f"{len(changed)} of {len(cmp_rows)} applications change proposed status under v{b['rule_version']}."]
    except rw.WorkflowError:
        pass

    path = BASE / "lab_report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
