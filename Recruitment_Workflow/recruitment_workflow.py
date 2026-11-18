"""Campus Placement Screening Workflow - core engine.

Pipeline: ingest -> validate/normalise -> score (rules.json) -> route -> draft notification
          -> human review (RBAC) -> simulated dispatch -> append-only audit log.

Design rules (from the lab brief):
  * scoring criteria live in rules.json, never in code or in candidate-supplied text
  * the system only PROPOSES a status; a human reviewer makes the decision
  * invalid records are quarantined with a reason, never silently dropped
  * the audit log is append-only and hash-chained so tampering is detectable
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable

from notifications import (
    DRAFT,
    NOT_SENT,
    dispatch_with_retry,
    generate_notification,
    validate_message,
)

BASE_DIR = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------------------------
REQUIRED_FIELDS = [
    "candidate_id", "candidate_name", "email", "branch", "cgpa",
    "active_backlogs", "aptitude_score", "coding_score", "application_role",
]
OPTIONAL_FIELDS = ["project_score", "candidate_notes"]
ALL_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

STATUS_SHORTLIST = "SHORTLIST_REVIEW"
STATUS_MANUAL = "MANUAL_REVIEW"
STATUS_NOT_SHORTLISTED = "NOT_SHORTLISTED_REVIEW"
ROUTING_STATUSES = (STATUS_SHORTLIST, STATUS_MANUAL, STATUS_NOT_SHORTLISTED)

REVIEW_ACTIONS = ("APPROVE", "EDIT", "HOLD", "REQUEST_INFO")
OPEN_REVIEW_STATES = ("ON_HOLD", "AWAITING_INFORMATION", "REQUIRES_REVIEW")  # may be re-reviewed

CRITERIA_LABELS = {
    "coding_score": "Coding test",
    "aptitude_score": "Aptitude test",
    "cgpa": "Academic performance (CGPA x 10)",
    "project_score": "Projects / internships",
}

BRANCH_ALIASES = {
    "COMPUTER": "CS", "COMPUTER ENGINEERING": "CS", "COMPUTER SCIENCE": "CS", "CSE": "CS", "COMPS": "CS",
    "INFORMATION TECHNOLOGY": "IT",
    "AI&DS": "AIDS", "AI & DS": "AIDS", "AI AND DS": "AIDS", "AIML": "AIDS",
    "ELECTRONICS AND TELECOMMUNICATION": "ENTC", "ELECTRONICS & TELECOM": "ENTC",
    "ELECTRONICS": "EXTC", "MECHANICAL": "MECH", "CIVIL ENGINEERING": "CIVIL",
}

ID_RE = re.compile(r"^[A-Z0-9_-]{2,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_NOTE_FLAG_RE = re.compile(
    r"(ignore|override|disregard|bypass|forget).{0,40}(rule|score|criteria|instruction|threshold)"
    r"|(set|give|make|assign).{0,20}score.{0,20}(100|max|full)"
    r"|shortlist me|auto[- ]?(approve|shortlist)|system prompt|you are now",
    re.IGNORECASE,
)
MAX_FILE_BYTES = 10 * 1024 * 1024


class WorkflowError(Exception):
    """Base class for workflow errors."""


class DataFileError(WorkflowError):
    """The candidate file is missing, unreadable or malformed."""


class RulesError(WorkflowError):
    """The scoring configuration is missing or malformed."""


class PermissionDenied(WorkflowError):
    """The user is not allowed to perform the requested action."""


# ----------------------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean_text(value) -> str:
    """Strip control characters, collapse whitespace."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", _CTRL_RE.sub(" ", str(value))).strip()


def csv_safe(value):
    """Neutralise spreadsheet formula injection (=, +, -, @) in values written to CSV."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def parse_number(value) -> Decimal | None:
    """Parse a finite number or return None."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


# ----------------------------------------------------------------------------------------------
# Rules (scoring configuration)
# ----------------------------------------------------------------------------------------------
def load_rules(path) -> dict:
    """Load and validate a rules file. Raises RulesError for anything missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            rules = json.load(fh)
    except FileNotFoundError:
        raise RulesError(f"Rules file not found: {path}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RulesError(f"Rules file is not valid JSON: {exc}")
    return validate_rules(rules)


def validate_rules(rules) -> dict:
    if not isinstance(rules, dict):
        raise RulesError("Rules must be a JSON object")
    for key in ("rule_version", "criteria", "thresholds", "eligibility"):
        if key not in rules:
            raise RulesError(f"Rules missing section: {key}")
    if not str(rules["rule_version"]).strip():
        raise RulesError("rule_version must not be empty")

    criteria = rules["criteria"]
    if not isinstance(criteria, dict) or not criteria:
        raise RulesError("criteria must be a non-empty object")
    unknown = set(criteria) - set(CRITERIA_LABELS)
    if unknown:
        raise RulesError(f"Unknown or disallowed scoring criteria: {sorted(unknown)}")
    total = Decimal(0)
    for name, weight in criteria.items():
        w = parse_number(weight) if not isinstance(weight, bool) else None
        if w is None or w < 0:
            raise RulesError(f"Weight for {name} must be a non-negative number")
        total += w
    if total != Decimal(100):
        raise RulesError(f"Criteria weights must add up to 100 (got {total})")

    th = rules["thresholds"]
    try:
        hi = Decimal(str(th["shortlist_review"]))
        lo = Decimal(str(th["manual_review"]))
    except (KeyError, InvalidOperation, TypeError):
        raise RulesError("thresholds need numeric shortlist_review and manual_review")
    if not (Decimal(0) <= lo < hi <= Decimal(100)):
        raise RulesError("thresholds must satisfy 0 <= manual_review < shortlist_review <= 100")

    el = rules["eligibility"]
    if not isinstance(el, dict):
        raise RulesError("eligibility must be an object")
    mc = parse_number(el.get("min_cgpa"))
    if mc is None or not (Decimal(0) <= mc <= Decimal(10)):
        raise RulesError("eligibility.min_cgpa must be between 0 and 10")
    mb = parse_number(el.get("max_active_backlogs"))
    if mb is None or mb < 0 or mb != mb.to_integral_value():
        raise RulesError("eligibility.max_active_backlogs must be a non-negative integer")
    if not isinstance(el.get("allowed_branches"), list) or not el["allowed_branches"]:
        raise RulesError("eligibility.allowed_branches must be a non-empty list")
    return rules


# ----------------------------------------------------------------------------------------------
# Step 3 - ingestion
# ----------------------------------------------------------------------------------------------
def load_candidates(file_path, verbose: bool = False) -> list[dict]:
    """Read candidate rows from CSV. Each row gets a ``_row`` number (header = row 1).

    Raises DataFileError when the file is missing, empty, oversized, undecodable or lacks required headers.
    """
    path = Path(file_path)
    if not path.is_file():
        raise DataFileError(f"Candidate file not found: {file_path}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DataFileError("Candidate file is larger than the 10 MB limit")
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, restkey="__extra__", restval=None)
            if not reader.fieldnames:
                raise DataFileError("Candidate file is empty or has no header row")
            reader.fieldnames = [clean_text(h).lower() for h in reader.fieldnames]
            missing = [h for h in REQUIRED_FIELDS if h not in reader.fieldnames]
            if missing:
                raise DataFileError(f"Candidate file is missing required column(s): {', '.join(missing)}")
            rows = []
            for line_no, row in enumerate(reader, start=2):
                row["_row"] = line_no
                rows.append(row)
    except DataFileError:
        raise
    except (csv.Error, UnicodeDecodeError) as exc:
        raise DataFileError(f"Malformed candidate file: {exc}")
    if verbose:
        print("Total records loaded:", len(rows))
    return rows


# ----------------------------------------------------------------------------------------------
# Step 4 - validation, normalisation, duplicate detection
# ----------------------------------------------------------------------------------------------
def normalize_candidate(raw: dict) -> dict:
    """Trim/clean every field and canonicalise id, email and branch."""
    c = {f: clean_text(raw.get(f)) for f in ALL_FIELDS}
    c["candidate_id"] = c["candidate_id"].upper()
    c["email"] = c["email"].lower()
    branch = c["branch"].upper()
    c["branch"] = BRANCH_ALIASES.get(branch, branch)
    return c


def validation_errors(c: dict) -> list[tuple[str, str]]:
    """Return a list of (category, message). Empty list means the record is valid."""
    errors: list[tuple[str, str]] = []
    for f in REQUIRED_FIELDS:
        if not c.get(f):
            errors.append(("MISSING_FIELD", f"Missing field: {f}"))

    if c.get("candidate_id") and not ID_RE.match(c["candidate_id"]):
        errors.append(("INVALID_VALUE", "Invalid candidate_id format"))

    ranges = {"cgpa": (0, 10), "aptitude_score": (0, 100), "coding_score": (0, 100), "project_score": (0, 100)}
    for f, (lo, hi) in ranges.items():
        raw = c.get(f, "")
        if raw == "":
            continue  # missing required handled above; project_score is optional
        num = parse_number(raw)
        if num is None:
            errors.append(("INVALID_VALUE", f"Invalid numeric value: {f}"))
        elif not (lo <= num <= hi):
            errors.append(("OUT_OF_RANGE", f"{f} must be between {lo} and {hi}"))

    if c.get("active_backlogs", "") != "":
        num = parse_number(c["active_backlogs"])
        if num is None or num != num.to_integral_value():
            errors.append(("INVALID_VALUE", "Invalid numeric value: active_backlogs"))
        elif not (0 <= num <= 50):
            errors.append(("OUT_OF_RANGE", "active_backlogs must be between 0 and 50"))

    if c.get("email") and not EMAIL_RE.match(c["email"]):
        errors.append(("INVALID_EMAIL", "Invalid email format"))
    return errors


def validate_candidate(c: dict) -> tuple[bool, str]:
    """Return (is_valid, message) for a normalised record (same contract as the lab manual)."""
    errors = validation_errors(c)
    if not errors:
        return True, "Valid record"
    return False, "; ".join(msg for _, msg in errors)


def record_hash(c: dict) -> str:
    payload = json.dumps({f: c.get(f, "") for f in ALL_FIELDS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_records(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Normalise + validate every row. Returns (valid, rejected).

    Duplicates are never silently deleted: the first valid occurrence is kept, later ones are
    quarantined as DUPLICATE_ID with a note saying whether it is an identical re-import or a
    conflicting resubmission.
    """
    valid: list[dict] = []
    rejected: list[dict] = []
    seen: dict[str, tuple[int, str]] = {}
    for raw in rows:
        row_no = raw.get("_row", 0)
        c = normalize_candidate(raw)
        c["_row"] = row_no

        if "__extra__" in raw or any(raw.get(f) is None for f in REQUIRED_FIELDS):
            rejected.append(_reject(raw, c, "MALFORMED_ROW", "Malformed row: wrong number of columns"))
            continue

        errors = validation_errors(c)
        if errors:
            rejected.append(_reject(raw, c, errors[0][0], "; ".join(m for _, m in errors)))
            continue

        h = record_hash(c)
        cid = c["candidate_id"]
        if cid in seen:
            first_row, first_hash = seen[cid]
            kind = "identical repeat import" if first_hash == h else "conflicting resubmission"
            rejected.append(_reject(
                raw, c, "DUPLICATE_ID",
                f"Duplicate candidate_id {cid} (first seen at row {first_row}); {kind}",
            ))
            continue
        seen[cid] = (row_no, h)
        c["_hash"] = h
        valid.append(c)
    return valid, rejected


def _reject(raw: dict, c: dict, category: str, reason: str) -> dict:
    snapshot = {f: clean_text(raw.get(f)) for f in ALL_FIELDS}
    return {
        "row_number": c.get("_row", ""),
        "candidate_id": c.get("candidate_id", ""),
        "category": category,
        "reason": reason,
        "raw_record": json.dumps(snapshot, ensure_ascii=False),
    }


REJECTED_FIELDS = ["timestamp", "run_id", "row_number", "candidate_id", "category", "reason", "record_hash", "raw_record"]


def write_rejected(rejected_path, rejected: list[dict], run_id: str) -> int:
    """Append quarantined records (skipping exact repeats of an already-quarantined record)."""
    path = Path(rejected_path)
    existing: set[str] = set()
    if path.is_file() and path.stat().st_size:
        with open(path, "r", newline="", encoding="utf-8") as fh:
            existing = {r.get("record_hash", "") for r in csv.DictReader(fh)}
    new_file = not path.is_file() or path.stat().st_size == 0
    written = 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REJECTED_FIELDS)
        if new_file:
            writer.writeheader()
        for r in rejected:
            h = hashlib.sha256(f"{r['row_number']}|{r['reason']}|{r['raw_record']}".encode()).hexdigest()
            if h in existing:
                continue
            existing.add(h)
            writer.writerow({
                "timestamp": now_iso(), "run_id": run_id, "row_number": r["row_number"],
                "candidate_id": csv_safe(r["candidate_id"]), "category": r["category"],
                "reason": csv_safe(r["reason"]), "record_hash": h, "raw_record": r["raw_record"],
            })
            written += 1
    return written


# ----------------------------------------------------------------------------------------------
# Steps 5-7 - scoring, explanation, routing
# ----------------------------------------------------------------------------------------------
def calculate_score(candidate: dict, rules: dict) -> dict:
    """Weighted score out of 100 with a criterion-wise breakdown (Decimal arithmetic: no float drift)."""
    breakdown = []
    total = Decimal(0)
    for key, weight in rules["criteria"].items():
        w = Decimal(str(weight))
        note = ""
        if key == "cgpa":
            value = parse_number(candidate["cgpa"]) * 10
        elif key == "project_score" and str(candidate.get("project_score", "")).strip() == "":
            value, note = Decimal(0), "missing - scored as 0 pending reviewer check"
        else:
            value = parse_number(candidate[key])
        contribution = value * w / 100
        total += contribution
        breakdown.append({
            "criterion": key,
            "label": CRITERIA_LABELS[key],
            "input": float(value),
            "weight": float(w),
            "contribution": float(contribution.quantize(Decimal("0.01"), ROUND_HALF_UP)),
            "max": float(w),
            "note": note,
        })
    score = total.quantize(Decimal("0.01"), ROUND_HALF_UP)
    if not (Decimal(0) <= score <= Decimal(100)):
        raise WorkflowError(f"Computed score {score} is outside 0-100")
    return {"score": float(score), "breakdown": breakdown, "rule_version": str(rules["rule_version"])}


def explain_score(candidate_id: str, result: dict) -> str:
    lines = [f"Candidate ID: {candidate_id}", f"Overall Score: {result['score']:g}/100"]
    for b in result["breakdown"]:
        extra = f"  [{b['note']}]" if b["note"] else ""
        lines.append(f"{b['label']} contribution: {b['contribution']:g}/{b['max']:g}{extra}")
    lines.append(f"Rule Version: {result['rule_version']}")
    return "\n".join(lines)


def route_candidate(score: float, thresholds: dict | None = None) -> str:
    """Pure score -> status routing (boundaries are inclusive on the lower edge)."""
    hi = Decimal(str((thresholds or {}).get("shortlist_review", 75)))
    lo = Decimal(str((thresholds or {}).get("manual_review", 50)))
    s = Decimal(str(score))
    if s >= hi:
        return STATUS_SHORTLIST
    if s >= lo:
        return STATUS_MANUAL
    return STATUS_NOT_SHORTLISTED


def check_eligibility(c: dict, rules: dict) -> dict:
    """Hard failures push towards NOT_SHORTLISTED_REVIEW; ambiguity forces MANUAL_REVIEW."""
    el = rules["eligibility"]
    hard, ambiguous = [], []
    cgpa = parse_number(c["cgpa"])
    if cgpa < Decimal(str(el["min_cgpa"])):
        hard.append(f"CGPA {cgpa:g} is below the minimum {el['min_cgpa']}")
    backlogs = int(parse_number(c["active_backlogs"]))
    if backlogs > int(el["max_active_backlogs"]):
        hard.append(f"{backlogs} active backlog(s) exceeds the maximum {el['max_active_backlogs']}")
    allowed = {str(b).upper() for b in el["allowed_branches"]}
    if c["branch"] not in allowed:
        ambiguous.append(f"Branch {c['branch']} is not in the listed eligible branches")
    if rules.get("role") and c["application_role"].strip().lower() != str(rules["role"]).strip().lower():
        ambiguous.append(f"Applied role '{c['application_role']}' differs from '{rules['role']}'")
    if "project_score" in rules["criteria"] and str(c.get("project_score", "")).strip() == "":
        ambiguous.append("Project/internship score missing - incomplete application")
    return {"hard_fail": hard, "ambiguous": ambiguous}


def scan_candidate_notes(text: str) -> bool:
    """True when free text looks like an attempt to influence scoring. Notes never affect the score."""
    return bool(text and _NOTE_FLAG_RE.search(text))


def evaluate_candidate(c: dict, rules: dict) -> dict:
    """Score + eligibility + routing for one valid record. Pure function (no I/O)."""
    result = calculate_score(c, rules)
    elig = check_eligibility(c, rules)
    base = route_candidate(result["score"], rules["thresholds"])
    reasons = []
    if elig["hard_fail"]:
        status = STATUS_NOT_SHORTLISTED
        reasons += [f"ELIGIBILITY: {m}" for m in elig["hard_fail"]]
    elif elig["ambiguous"]:
        status = STATUS_MANUAL
        reasons += [f"AMBIGUOUS: {m}" for m in elig["ambiguous"]]
    else:
        status = base
    if scan_candidate_notes(c.get("candidate_notes", "")):
        reasons.append("NOTES_FLAGGED: free text appears to try to influence scoring; it was ignored")
    return {
        "score": result["score"],
        "breakdown": result["breakdown"],
        "rule_version": result["rule_version"],
        "score_status": base,
        "proposed_status": status,
        "flags": reasons,
    }


# ----------------------------------------------------------------------------------------------
# Step 10 - append-only, hash-chained audit log
# ----------------------------------------------------------------------------------------------
AUDIT_FIELDS = [
    "timestamp", "run_id", "event", "candidate_id", "validation_status", "score", "score_breakdown",
    "rule_version", "proposed_status", "reviewer_id", "reviewer_role", "reviewer_action", "final_status",
    "notification_status", "notes", "error_details", "prev_hash", "record_hash",
]
_AUDIT_LOCK = threading.Lock()
GENESIS = "0" * 64


class AuditLog:
    """CSV audit trail. Opened in append mode only; every row embeds the hash of the previous row."""

    def __init__(self, path):
        self.path = Path(path)

    def _last_hash(self) -> str:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return GENESIS
        with open(self.path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != AUDIT_FIELDS:
                raise WorkflowError("audit_log.csv has an unexpected header; refusing to append")
            last = GENESIS
            for row in reader:
                last = row["record_hash"]
            return last

    @staticmethod
    def _hash(prev: str, row: dict) -> str:
        body = json.dumps([row[f] for f in AUDIT_FIELDS if f not in ("prev_hash", "record_hash")],
                          ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()

    def append(self, event: str, **fields) -> dict:
        row = {f: "" for f in AUDIT_FIELDS}
        row["timestamp"] = now_iso()
        row["event"] = event
        for k, v in fields.items():
            if k not in row:
                raise KeyError(f"Unknown audit field: {k}")
            row[k] = csv_safe("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else str(v)))
        with _AUDIT_LOCK:
            row["prev_hash"] = self._last_hash()
            row["record_hash"] = self._hash(row["prev_hash"], row)
            new_file = not self.path.is_file() or self.path.stat().st_size == 0
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=AUDIT_FIELDS)
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                fh.flush()
                os.fsync(fh.fileno())
        return row

    def read(self) -> list[dict]:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return []
        with open(self.path, "r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))

    def verify(self) -> tuple[bool, int, str]:
        """Re-compute the hash chain. Returns (ok, rows_checked, message)."""
        rows = self.read()
        prev = GENESIS
        for i, row in enumerate(rows, start=1):
            if row["prev_hash"] != prev or row["record_hash"] != self._hash(prev, row):
                return False, i, f"Audit chain broken at row {i} (event {row.get('event')})"
            prev = row["record_hash"]
        return True, len(rows), "Audit chain intact"


# ----------------------------------------------------------------------------------------------
# Workflow state (SQLite)
# ----------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    candidate_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
    candidate_name TEXT, email TEXT, branch TEXT, cgpa REAL, active_backlogs INTEGER,
    aptitude_score REAL, coding_score REAL, project_score REAL, application_role TEXT, candidate_notes TEXT,
    score REAL, score_breakdown TEXT, rule_version TEXT, rules_file TEXT,
    proposed_status TEXT, flags TEXT,
    review_action TEXT, reviewer_id TEXT, reviewer_role TEXT, review_notes TEXT, review_ts TEXT,
    final_status TEXT, override INTEGER DEFAULT 0,
    notif_subject TEXT, notif_body TEXT, notif_state TEXT, notif_attempts INTEGER DEFAULT 0, notif_error TEXT,
    processed_ts TEXT, run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started TEXT, rules_file TEXT, rule_version TEXT, summary TEXT
);
"""


@contextmanager
def db_conn(db_path):
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path) -> None:
    with db_conn(db_path) as conn:
        conn.executescript(SCHEMA)


def get_application(db_path, candidate_id: str) -> dict | None:
    with db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM applications WHERE candidate_id=?", (candidate_id,)).fetchone()
    return dict(row) if row else None


def list_applications(db_path, status: str | None = None) -> list[dict]:
    init_db(db_path)
    sql, args = "SELECT * FROM applications", ()
    if status:
        sql += " WHERE proposed_status=?"
        args = (status,)
    with db_conn(db_path) as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY candidate_id", args)]


def application_as_candidate(app: dict) -> dict:
    """Rebuild a string-valued candidate dict from a stored row (for re-scoring / comparisons)."""
    c = {}
    for f in ALL_FIELDS:
        v = app.get(f)
        c[f] = "" if v is None else (str(int(v)) if f == "active_backlogs" else str(v))
    return c


def _upsert_application(conn, c, result, rules_file, draft, run_id):
    conn.execute("DELETE FROM applications WHERE candidate_id=?", (c["candidate_id"],))
    conn.execute(
        """INSERT INTO applications (candidate_id, input_hash, candidate_name, email, branch, cgpa, active_backlogs,
           aptitude_score, coding_score, project_score, application_role, candidate_notes, score, score_breakdown,
           rule_version, rules_file, proposed_status, flags, notif_subject, notif_body, notif_state, notif_attempts,
           processed_ts, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            c["candidate_id"], c["_hash"], c["candidate_name"], c["email"], c["branch"],
            float(parse_number(c["cgpa"])), int(parse_number(c["active_backlogs"])),
            float(parse_number(c["aptitude_score"])), float(parse_number(c["coding_score"])),
            None if c["project_score"] == "" else float(parse_number(c["project_score"])),
            c["application_role"], c["candidate_notes"], result["score"], json.dumps(result["breakdown"]),
            result["rule_version"], rules_file, result["proposed_status"], json.dumps(result["flags"]),
            draft["subject"], draft["message"], DRAFT, 0, now_iso(), run_id,
        ),
    )


# ----------------------------------------------------------------------------------------------
# Step 11 - the pipeline
# ----------------------------------------------------------------------------------------------
def run_pipeline(candidates_path, rules_path, db_path, audit_path, rejected_path,
                 actor: str = "system", run_id: str | None = None, verbose: bool = False) -> dict:
    """Ingest, validate, score, route and draft notifications. Never makes a final decision.

    Aborts (raising DataFileError / RulesError) before any scoring if inputs cannot be trusted.
    Re-running on the same records is idempotent: unchanged applications are skipped.
    """
    t0 = time.perf_counter()
    run_id = run_id or f"RUN-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    audit = AuditLog(audit_path)
    init_db(db_path)
    audit.append("RUN_STARTED", run_id=run_id,
                 notes=f"actor={actor}; file={Path(candidates_path).name}; rules={Path(rules_path).name}")

    try:
        rules = load_rules(rules_path)
    except RulesError as exc:
        audit.append("RULES_ERROR", run_id=run_id, error_details=str(exc), notes="Run aborted before scoring")
        raise
    try:
        rows = load_candidates(candidates_path, verbose=verbose)
    except DataFileError as exc:
        audit.append("FILE_ERROR", run_id=run_id, error_details=str(exc), notes="Run aborted before scoring")
        raise

    valid, rejected = split_records(rows)
    write_rejected(rejected_path, rejected, run_id)
    for r in rejected:
        audit.append("QUARANTINED", run_id=run_id, candidate_id=r["candidate_id"], validation_status="INVALID",
                     error_details=r["reason"], notes=f"row {r['row_number']}; category {r['category']}")

    counts = {s: 0 for s in ROUTING_STATUSES}
    processed = skipped = failed = drafts = updated = 0
    rules_file = Path(rules_path).name

    for c in valid:
        cid = c["candidate_id"]
        try:
            existing = get_application(db_path, cid)
            if existing and existing["input_hash"] == c["_hash"]:
                skipped += 1
                audit.append("SKIPPED_ALREADY_PROCESSED", run_id=run_id, candidate_id=cid,
                             validation_status="VALID", notes="Identical record already processed (idempotent re-run)")
                continue
            if existing:
                updated += 1
                audit.append("APPLICATION_UPDATED", run_id=run_id, candidate_id=cid, validation_status="VALID",
                             notes="Record content changed since last run; re-scored and review reset")
            audit.append("INGESTED", run_id=run_id, candidate_id=cid, validation_status="VALID")

            result = evaluate_candidate(c, rules)
            draft = generate_notification(c, result["proposed_status"], company=rules.get("company", "the visiting company"))
            with db_conn(db_path) as conn:
                _upsert_application(conn, c, result, rules_file, draft, run_id)
            drafts += 1
            counts[result["proposed_status"]] += 1
            processed += 1
            audit.append(
                "SCORED_AND_ROUTED", run_id=run_id, candidate_id=cid, validation_status="VALID",
                score=result["score"],
                score_breakdown="; ".join(f"{b['criterion']}={b['contribution']:g}" for b in result["breakdown"]),
                rule_version=result["rule_version"], proposed_status=result["proposed_status"],
                notification_status=DRAFT, notes=" | ".join(result["flags"]),
            )
        except Exception as exc:  # one bad record must not stop the batch
            failed += 1
            audit.append("PROCESSING_ERROR", run_id=run_id, candidate_id=cid, validation_status="VALID",
                         error_details=f"{type(exc).__name__}: {exc}")

    elapsed = time.perf_counter() - t0
    dup = sum(1 for r in rejected if r["category"] == "DUPLICATE_ID")
    summary = {
        "run_id": run_id, "rules_file": rules_file, "rule_version": str(rules["rule_version"]),
        "total_records": len(rows), "valid_records": len(valid), "invalid_records": len(rejected),
        "duplicates_detected": dup, "processed": processed, "skipped_already_processed": skipped,
        "updated_applications": updated, "failed": failed, "drafts_generated": drafts,
        "shortlist_review": counts[STATUS_SHORTLIST], "manual_review": counts[STATUS_MANUAL],
        "not_shortlisted_review": counts[STATUS_NOT_SHORTLISTED],
        "elapsed_seconds": round(elapsed, 4),
        "avg_ms_per_valid_record": round(elapsed * 1000 / processed, 3) if processed else None,
    }
    with db_conn(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                     (run_id, now_iso(), rules_file, summary["rule_version"], json.dumps(summary)))
    audit.append("RUN_COMPLETED", run_id=run_id, rule_version=summary["rule_version"],
                 notes=json.dumps({k: summary[k] for k in ("total_records", "valid_records", "invalid_records",
                                                           "processed", "skipped_already_processed", "failed")}))
    return summary


# ----------------------------------------------------------------------------------------------
# Access control (variation 9: HR reviewer vs administrator)
# ----------------------------------------------------------------------------------------------
PERMISSIONS = {
    "PLACEMENT_COORDINATOR": {"view", "review", "view_audit"},
    "ADMIN": {"view", "run_pipeline", "view_audit", "manage_rules"},
    "VIEWER": {"view"},
}
DEFAULT_USERS = {
    "coordinator1": ("Placement@123", "PLACEMENT_COORDINATOR"),
    "coordinator2": ("Placement@456", "PLACEMENT_COORDINATOR"),
    "admin": ("Admin@123", "ADMIN"),
    "viewer": ("Viewer@123", "VIEWER"),
}


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex() + "$" + digest.hex()


def ensure_default_users(path) -> None:
    """Create users.json with salted PBKDF2 hashes for the lab's demo accounts (no plaintext stored)."""
    p = Path(path)
    if p.is_file():
        return
    users = {u: {"role": role, "password_hash": _hash_password(pw)} for u, (pw, role) in DEFAULT_USERS.items()}
    p.write_text(json.dumps(users, indent=2), encoding="utf-8")


def authenticate(username: str, password: str, users_path) -> dict | None:
    ensure_default_users(users_path)
    users = json.loads(Path(users_path).read_text(encoding="utf-8"))
    rec = users.get(str(username).strip())
    if not rec:
        _hash_password(password)  # keep timing similar for unknown users
        return None
    salt_hex, digest_hex = rec["password_hash"].split("$")
    candidate = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), bytes.fromhex(salt_hex), 120_000)
    if hmac.compare_digest(candidate.hex(), digest_hex):
        return {"user_id": str(username).strip(), "role": rec["role"]}
    return None


def authorize(user: dict | None, permission: str) -> bool:
    return bool(user) and permission in PERMISSIONS.get(user.get("role", ""), set())


def _user_is_genuine(user: dict, users_path) -> bool:
    """Guard against a forged role: the stored role for that user id must match."""
    if not users_path:
        return True
    try:
        users = json.loads(Path(users_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    rec = users.get(user.get("user_id", ""))
    return bool(rec) and rec["role"] == user.get("role")


# ----------------------------------------------------------------------------------------------
# Step 9 - human-in-the-loop review
# ----------------------------------------------------------------------------------------------
def review_action(proposed_status: str, action: str, new_status: str | None = None) -> str:
    """Map a reviewer action to the resulting workflow status (extends the lab-manual function)."""
    if action == "APPROVE":
        return proposed_status
    if action == "EDIT":
        return new_status if new_status else "REQUIRES_REVIEW"
    if action == "HOLD":
        return "ON_HOLD"
    if action == "REQUEST_INFO":
        return "AWAITING_INFORMATION"
    raise ValueError("Invalid review action")


def _deny(audit: AuditLog, candidate_id, uid, role, action, reason):
    audit.append("UNAUTHORIZED_ACTION", candidate_id=candidate_id, reviewer_id=uid, reviewer_role=role,
                 reviewer_action=action, notification_status=NOT_SENT, error_details="PermissionDenied", notes=reason)
    raise PermissionDenied(reason)


def apply_review(db_path, audit_path, candidate_id: str, user: dict, action: str, notes: str = "",
                 new_status: str | None = None, edited_message: str | None = None,
                 sender: Callable | None = None, max_retries: int = 2, users_path=None) -> dict:
    """Record a human decision, then (only if approved) simulate sending the approved message."""
    audit = AuditLog(audit_path)
    user = user or {}
    uid, role = user.get("user_id", "unknown"), user.get("role", "unknown")
    action = str(action).strip().upper()
    candidate_id = str(candidate_id).strip().upper()

    if not _user_is_genuine(user, users_path) or not authorize(user, "review"):
        _deny(audit, candidate_id, uid, role, action, f"Role '{role}' may not approve or modify decisions")

    app = get_application(db_path, candidate_id)
    if app is None:
        audit.append("REVIEW_REJECTED", candidate_id=candidate_id, reviewer_id=uid, reviewer_role=role,
                     reviewer_action=action, error_details="Unknown candidate")
        raise WorkflowError(f"Unknown candidate: {candidate_id}")

    def refuse(message: str, exc=ValueError):
        audit.append("REVIEW_REJECTED", candidate_id=candidate_id, reviewer_id=uid, reviewer_role=role,
                     reviewer_action=action, proposed_status=app["proposed_status"], error_details=message)
        raise exc(message)

    if action not in REVIEW_ACTIONS:
        refuse("Invalid review action")
    if app["review_action"] and app["final_status"] not in OPEN_REVIEW_STATES:
        refuse(f"Application already finalised as {app['final_status']}; no second decision or duplicate notification",
               WorkflowError)

    notes = clean_text(notes)
    if action in ("EDIT", "REQUEST_INFO") and len(notes) < 5:
        refuse(f"{action} requires a documented reason (at least 5 characters)")

    proposed = app["proposed_status"]
    subject, body = app["notif_subject"], app["notif_body"]
    send = False
    override = 0

    if action == "APPROVE":
        final, send = proposed, True
    elif action == "EDIT":
        if new_status is None and not edited_message:
            final = review_action(proposed, "EDIT")  # REQUIRES_REVIEW
        else:
            if new_status is not None and new_status not in ROUTING_STATUSES:
                refuse(f"Invalid status '{new_status}'")
            final = new_status or proposed
            override = int(final != proposed)
            candidate = application_as_candidate(app)
            if final != proposed:
                draft = generate_notification(candidate, final)
                subject, body = draft["subject"], draft["message"]
            if edited_message:
                try:
                    body = validate_message(edited_message, final)
                except ValueError as exc:
                    refuse(str(exc))
            send = True
    else:
        final = review_action(proposed, action)

    ts = now_iso()
    if send:
        state, attempts, error = dispatch_with_retry({"candidate_id": candidate_id, "subject": subject, "message": body},
                                                      app["email"], sender=sender, max_retries=max_retries)
    else:
        state, attempts, error = NOT_SENT, 0, ""

    with db_conn(db_path) as conn:
        conn.execute(
            """UPDATE applications SET review_action=?, reviewer_id=?, reviewer_role=?, review_notes=?, review_ts=?,
               final_status=?, override=?, notif_subject=?, notif_body=?, notif_state=?, notif_attempts=?, notif_error=?
               WHERE candidate_id=?""",
            (action, uid, role, notes, ts, final, override, subject, body, state, attempts, error, candidate_id),
        )
    audit.append("REVIEW_DECISION", candidate_id=candidate_id, score=app["score"], rule_version=app["rule_version"],
                 proposed_status=proposed, reviewer_id=uid, reviewer_role=role, reviewer_action=action,
                 final_status=final, notification_status=state, notes=(("OVERRIDE: " if override else "") + notes),
                 error_details=error)
    if send:
        audit.append("NOTIFICATION", candidate_id=candidate_id, reviewer_id=uid, reviewer_role=role,
                     final_status=final, notification_status=state, notes=f"attempts={attempts}", error_details=error)
    return {"candidate_id": candidate_id, "action": action, "proposed_status": proposed, "final_status": final,
            "override": bool(override), "notification_status": state, "attempts": attempts, "error": error}


def retry_notification(db_path, audit_path, candidate_id: str, user: dict, sender=None,
                       max_retries: int = 2, users_path=None) -> dict:
    """Re-attempt a FAILED notification for an already-approved decision."""
    audit = AuditLog(audit_path)
    uid, role = (user or {}).get("user_id", "unknown"), (user or {}).get("role", "unknown")
    candidate_id = str(candidate_id).strip().upper()
    if not _user_is_genuine(user or {}, users_path) or not authorize(user, "review"):
        _deny(audit, candidate_id, uid, role, "RETRY_NOTIFICATION", f"Role '{role}' may not resend notifications")
    app = get_application(db_path, candidate_id)
    if not app or app["notif_state"] != "FAILED":
        raise WorkflowError("Only notifications in FAILED state can be retried")
    state, attempts, error = dispatch_with_retry(
        {"candidate_id": candidate_id, "subject": app["notif_subject"], "message": app["notif_body"]},
        app["email"], sender=sender, max_retries=max_retries)
    with db_conn(db_path) as conn:
        conn.execute("UPDATE applications SET notif_state=?, notif_attempts=notif_attempts+?, notif_error=? WHERE candidate_id=?",
                     (state, attempts, error, candidate_id))
    audit.append("NOTIFICATION_RETRY", candidate_id=candidate_id, reviewer_id=uid, reviewer_role=role,
                 final_status=app["final_status"], notification_status=state, notes=f"attempts={attempts}", error_details=error)
    return {"candidate_id": candidate_id, "notification_status": state, "attempts": attempts, "error": error}


# ----------------------------------------------------------------------------------------------
# Evaluation: metrics, summaries, rule comparison, fairness review
# ----------------------------------------------------------------------------------------------
def status_counts(db_path) -> dict:
    apps = list_applications(db_path)
    out = {s: 0 for s in ROUTING_STATUSES}
    for a in apps:
        out[a["proposed_status"]] = out.get(a["proposed_status"], 0) + 1
    return out


def review_counts(db_path) -> dict:
    apps = list_applications(db_path)
    reviewed = [a for a in apps if a["review_action"]]
    out = {
        "total_applications": len(apps),
        "approved": sum(1 for a in reviewed if a["review_action"] == "APPROVE"),
        "edited": sum(1 for a in reviewed if a["review_action"] == "EDIT"),
        "held": sum(1 for a in reviewed if a["review_action"] == "HOLD"),
        "info_requested": sum(1 for a in reviewed if a["review_action"] == "REQUEST_INFO"),
        "overrides": sum(1 for a in apps if a["override"]),
        "completed": sum(1 for a in apps if a["review_action"] and a["final_status"] in ROUTING_STATUSES),
        "pending": sum(1 for a in apps if not a["review_action"] or a["final_status"] not in ROUTING_STATUSES),
        "notifications_sent": sum(1 for a in apps if a["notif_state"] == "SENT"),
        "notifications_failed": sum(1 for a in apps if a["notif_state"] == "FAILED"),
        "notifications_not_sent": sum(1 for a in apps if a["notif_state"] == "NOT_SENT"),
        "drafts_pending": sum(1 for a in apps if a["notif_state"] == "DRAFT"),
    }
    return out


def _pct(num, den):
    return round(100.0 * num / den, 2) if den else None


def compute_metrics(summary: dict, db_path, tests: dict | None = None) -> dict:
    """The six lab metrics (None = not applicable, e.g. nothing to divide by)."""
    rc = review_counts(db_path)
    done = summary["processed"] + summary["skipped_already_processed"]
    sent, failed = rc["notifications_sent"], rc["notifications_failed"]
    return {
        "data_validation_rate_pct": _pct(summary["valid_records"], summary["total_records"]),
        "workflow_success_rate_pct": _pct(done - 0, summary["valid_records"]),
        "draft_generation_rate_pct": _pct(summary["drafts_generated"], summary["processed"]),
        "test_pass_rate_pct": _pct(tests["passed"], tests["total"]) if tests else None,
        "avg_processing_ms_per_valid_record": summary["avg_ms_per_valid_record"],
        "human_review_completion_pct": _pct(rc["completed"], rc["total_applications"]),
        "notification_delivery_success_pct": _pct(sent, sent + failed),
    }


def compare_rule_versions(db_path, rules_a: dict, rules_b: dict) -> list[dict]:
    """Re-score every stored application under two rule sets (read-only; no state is changed)."""
    out = []
    for app in list_applications(db_path):
        c = application_as_candidate(app)
        a, b = evaluate_candidate(c, rules_a), evaluate_candidate(c, rules_b)
        out.append({
            "candidate_id": app["candidate_id"],
            "score_a": a["score"], "status_a": a["proposed_status"],
            "score_b": b["score"], "status_b": b["proposed_status"],
            "score_change": round(b["score"] - a["score"], 2),
            "status_changed": a["proposed_status"] != b["proposed_status"],
        })
    return out


def fairness_by_branch(db_path) -> list[dict]:
    """Shortlist-review rate per branch plus the four-fifths (80%) ratio against the best-treated branch.

    Branch is an eligibility input, so a gap here is expected; the table exists so a human can ask
    whether the configured criteria disadvantage a group. It does not prove or disprove fairness.
    """
    groups: dict[str, dict] = {}
    for a in list_applications(db_path):
        g = groups.setdefault(a["branch"], {"branch": a["branch"], "applications": 0, "shortlist_review": 0})
        g["applications"] += 1
        g["shortlist_review"] += int(a["proposed_status"] == STATUS_SHORTLIST)
    rows = list(groups.values())
    for r in rows:
        r["shortlist_rate_pct"] = _pct(r["shortlist_review"], r["applications"]) or 0.0
    best = max((r["shortlist_rate_pct"] for r in rows), default=0)
    for r in rows:
        r["ratio_to_best"] = round(r["shortlist_rate_pct"] / best, 2) if best else None
        r["below_four_fifths"] = bool(best and r["applications"] >= 5 and r["shortlist_rate_pct"] / best < 0.8)
    return sorted(rows, key=lambda r: -r["applications"])
