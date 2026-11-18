"""Tests for the Campus Placement Screening Workflow (maps to the lab's testing summary table).

Run:  pytest -q
Every test uses its own temporary directory, so the real audit log and database are never touched.
"""
import csv
import json
import shutil
from pathlib import Path

import pytest

import recruitment_workflow as rw
import notifications as nt

BASE = Path(__file__).resolve().parent
HEADER = ["candidate_id", "candidate_name", "email", "branch", "cgpa", "active_backlogs",
          "aptitude_score", "coding_score", "project_score", "application_role", "candidate_notes"]


def make_row(cid="T1", name="Test Student", email="t1@example.com", branch="CS", cgpa="8.0", backlogs="0",
             apt="80", code="80", proj="80", notes=""):
    return [cid, name, email, branch, cgpa, backlogs, apt, code, proj, "Software Engineer Trainee", notes]


def write_csv(path, rows, header=HEADER):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


class Env:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.db, self.audit = self.tmp / "w.db", self.tmp / "audit.csv"
        self.rejected, self.users = self.tmp / "rej.csv", self.tmp / "users.json"
        self.rules = BASE / "rules.json"
        rw.ensure_default_users(self.users)

    def run(self, rows, rules=None, header=HEADER):
        csv_path = write_csv(self.tmp / "in.csv", rows, header)
        return rw.run_pipeline(csv_path, rules or self.rules, self.db, self.audit, self.rejected)

    def login(self, name="coordinator1"):
        return rw.authenticate(name, rw.DEFAULT_USERS[name][0], self.users)

    def app(self, cid):
        return rw.get_application(self.db, cid)

    def events(self, name=None):
        rows = rw.AuditLog(self.audit).read()
        return [r for r in rows if not name or r["event"] == name]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# --- Ingestion ------------------------------------------------------------------------------------
def test_valid_record_loaded(env):
    s = env.run([make_row()])
    assert s["total_records"] == 1 and s["valid_records"] == 1 and s["invalid_records"] == 0
    assert env.app("T1")["score"] == 80.0


def test_bundled_dataset_has_at_least_100_records_and_special_cases():
    rows = rw.load_candidates(BASE / "candidates.csv")
    assert len(rows) >= 100
    valid, rejected = rw.split_records(rows)
    cats = {r["category"] for r in rejected}
    assert {"MISSING_FIELD", "DUPLICATE_ID", "OUT_OF_RANGE", "INVALID_VALUE", "INVALID_EMAIL"} <= cats
    assert len(valid) + len(rejected) == len(rows)


def test_missing_file_stops_before_scoring(env):
    with pytest.raises(rw.DataFileError):
        rw.run_pipeline(env.tmp / "nope.csv", env.rules, env.db, env.audit, env.rejected)
    assert env.events("FILE_ERROR") and rw.list_applications(env.db) == []


def test_missing_required_column_is_malformed(env):
    bad_header = [h for h in HEADER if h != "email"]
    path = write_csv(env.tmp / "bad.csv", [[c for c, h in zip(make_row(), HEADER) if h != "email"]], bad_header)
    with pytest.raises(rw.DataFileError, match="email"):
        rw.run_pipeline(path, env.rules, env.db, env.audit, env.rejected)


def test_binary_garbage_file_is_rejected(env):
    p = env.tmp / "garbage.csv"
    p.write_bytes(b"\xff\xfe\x00\x00\x80\x81 not csv \xff")
    with pytest.raises(rw.DataFileError):
        rw.run_pipeline(p, env.rules, env.db, env.audit, env.rejected)


def test_ragged_row_is_quarantined(env):
    p = env.tmp / "ragged.csv"
    p.write_text(",".join(HEADER) + "\nT1,Only Three,x@example.com\n" + ",".join(make_row("T2")) + "\n", encoding="utf-8")
    s = rw.run_pipeline(p, env.rules, env.db, env.audit, env.rejected)
    assert s["invalid_records"] == 1 and s["valid_records"] == 1


# --- Validation -------------------------------------------------------------------------------------
def test_missing_email_quarantined_with_reason(env):
    s = env.run([make_row(email="")])
    assert s["invalid_records"] == 1 and s["processed"] == 0
    row = next(csv.DictReader(open(env.rejected)))
    assert "Missing field: email" in row["reason"]


@pytest.mark.parametrize("kwargs", [
    dict(code="101"), dict(code="-1"), dict(apt="150"), dict(proj="-0.5"), dict(cgpa="10.1"),
    dict(cgpa="abc"), dict(apt="nan"), dict(code="inf"), dict(backlogs="-1"), dict(backlogs="1.5"),
])
def test_out_of_range_or_invalid_numbers_rejected(env, kwargs):
    s = env.run([make_row(**kwargs)])
    assert s["invalid_records"] == 1 and rw.list_applications(env.db) == []


def test_bad_email_and_bad_id_rejected(env):
    s = env.run([make_row(cid="A", email="no-at-sign"), make_row(cid="has space!", email="a@b.co")])
    assert s["invalid_records"] == 2


def test_whitespace_and_case_are_normalised(env):
    env.run([make_row(cid="  t9 ", name="  Asha   Rao ", email=" ASHA@Example.COM ", branch="computer")])
    a = env.app("T9")
    assert a["candidate_name"] == "Asha Rao" and a["email"] == "asha@example.com" and a["branch"] == "CS"


def test_duplicate_id_flagged_not_silently_deleted(env):
    s = env.run([make_row("D1"), make_row("D1", code="99"), make_row("D1")])
    assert s["duplicates_detected"] == 2 and s["valid_records"] == 1
    reasons = [r["reason"] for r in csv.DictReader(open(env.rejected))]
    assert any("conflicting resubmission" in r for r in reasons)
    assert any("identical repeat import" in r for r in reasons)
    assert len(env.events("QUARANTINED")) == 2


# --- Scoring ---------------------------------------------------------------------------------------
def test_score_matches_formula_and_breakdown_sums():
    c = {"cgpa": "8.0", "coding_score": "90", "aptitude_score": "70", "project_score": "50"}
    r = rw.calculate_score(c, rw.load_rules(BASE / "rules.json"))
    assert r["score"] == pytest.approx(0.35 * 90 + 0.25 * 70 + 0.20 * 80 + 0.20 * 50)
    assert sum(b["contribution"] for b in r["breakdown"]) == pytest.approx(r["score"], abs=0.02)
    assert r["rule_version"] == "1.0"


def test_score_always_within_0_and_100():
    rules = rw.load_rules(BASE / "rules.json")
    lo = rw.calculate_score({"cgpa": "0", "coding_score": "0", "aptitude_score": "0", "project_score": "0"}, rules)
    hi = rw.calculate_score({"cgpa": "10", "coding_score": "100", "aptitude_score": "100", "project_score": "100"}, rules)
    assert lo["score"] == 0 and hi["score"] == 100


def test_explanation_is_criterion_wise():
    rules = rw.load_rules(BASE / "rules.json")
    r = rw.calculate_score({"cgpa": "8", "coding_score": "80", "aptitude_score": "80", "project_score": "80"}, rules)
    text = rw.explain_score("C1", r)
    assert "Overall Score: 80/100" in text and "Coding test contribution" in text and "Rule Version: 1.0" in text


# --- Routing & boundaries ---------------------------------------------------------------------------
@pytest.mark.parametrize("score,expected", [
    (100, "SHORTLIST_REVIEW"), (82, "SHORTLIST_REVIEW"), (75, "SHORTLIST_REVIEW"),
    (74.99, "MANUAL_REVIEW"), (50, "MANUAL_REVIEW"),
    (49.99, "NOT_SHORTLISTED_REVIEW"), (0, "NOT_SHORTLISTED_REVIEW"),
])
def test_route_candidate_boundaries(score, expected):
    assert rw.route_candidate(score) == expected


def test_boundary_records_in_dataset_route_consistently(env):
    s = env.run(list(csv.reader(open(BASE / "candidates.csv")))[1:])
    got = {rw.get_application(env.db, i)["candidate_name"]: rw.get_application(env.db, i)
           for i in ("C183", "C184", "C185", "C186")}
    assert got["Boundary exactly 75"]["score"] == 75.0 and got["Boundary exactly 75"]["proposed_status"] == "SHORTLIST_REVIEW"
    assert got["Boundary exactly 50"]["proposed_status"] == "MANUAL_REVIEW"
    assert got["Boundary just below 75"]["proposed_status"] == "MANUAL_REVIEW"
    assert got["Boundary just below 50"]["proposed_status"] == "NOT_SHORTLISTED_REVIEW"
    assert s["valid_records"] + s["invalid_records"] == s["total_records"]


def test_eligibility_failure_never_reaches_shortlist(env):
    env.run([make_row("B1", backlogs="2", apt="99", code="99", proj="99", cgpa="9.5")])
    a = env.app("B1")
    assert a["score"] > 90 and a["proposed_status"] == "NOT_SHORTLISTED_REVIEW"
    assert any("ELIGIBILITY" in f for f in json.loads(a["flags"]))


def test_ambiguous_applications_go_to_manual_review(env):
    env.run([make_row("M1", branch="MECH", apt="99", code="99", proj="99"), make_row("M2", proj="")])
    assert env.app("M1")["proposed_status"] == "MANUAL_REVIEW"
    assert env.app("M2")["proposed_status"] == "MANUAL_REVIEW"
    assert env.app("M2")["project_score"] is None


def test_other_company_rules_change_outcome(env):
    env.run([make_row("S1", branch="MECH", backlogs="1", cgpa="5.8", apt="80", code="70", proj="60")], rules=BASE / "rules_service.json")
    a = env.app("S1")
    assert a["proposed_status"] in ("SHORTLIST_REVIEW", "MANUAL_REVIEW") and a["rules_file"] == "rules_service.json"


# --- Rules configuration ---------------------------------------------------------------------------
def test_missing_rules_file_aborts_run(env):
    with pytest.raises(rw.RulesError):
        rw.run_pipeline(write_csv(env.tmp / "c.csv", [make_row()]), env.tmp / "none.json", env.db, env.audit, env.rejected)
    assert env.events("RULES_ERROR") and rw.list_applications(env.db) == []


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["criteria"].update(coding_score=50), "add up to 100"),
    (lambda r: r["criteria"].update(gender=0), "Unknown or disallowed"),
    (lambda r: r["thresholds"].update(manual_review=90), "thresholds"),
    (lambda r: r.pop("eligibility"), "missing section"),
    (lambda r: r["criteria"].update(coding_score="lots"), "non-negative number"),
])
def test_malformed_rules_rejected(env, mutate, msg):
    rules = json.loads((BASE / "rules.json").read_text())
    mutate(rules)
    p = env.tmp / "bad_rules.json"
    p.write_text(json.dumps(rules))
    with pytest.raises(rw.RulesError, match=msg):
        rw.load_rules(p)


def test_invalid_json_rules(env):
    p = env.tmp / "r.json"
    p.write_text("{not json")
    with pytest.raises(rw.RulesError):
        rw.load_rules(p)


# --- Notifications ----------------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["SHORTLIST_REVIEW", "MANUAL_REVIEW", "NOT_SHORTLISTED_REVIEW"])
def test_notification_drafts(status):
    d = nt.generate_notification({"candidate_id": "C1", "candidate_name": "Asha Rao", "score": 91}, status)
    assert d["draft_status"] == "DRAFT" and "Asha Rao" in d["message"] and "C1" in d["message"]
    assert "91" not in d["message"] and "score" not in d["message"].lower()
    assert "reject" not in d["message"].lower()


def test_notification_name_cannot_inject_headers():
    d = nt.generate_notification({"candidate_id": "C1", "candidate_name": "Eve\r\nBcc: x@y.z"}, "MANUAL_REVIEW")
    assert "\r" not in d["message"].split("\n")[0]


def test_edited_message_guards():
    with pytest.raises(ValueError):
        nt.validate_message("Your score was 91 so congratulations on this progress.", "SHORTLIST_REVIEW")
    with pytest.raises(ValueError):
        nt.validate_message("We regret to inform you that you were rejected.", "NOT_SHORTLISTED_REVIEW")
    assert nt.validate_message("Your application is still under review by the cell.", "MANUAL_REVIEW")


def test_drafts_created_for_every_processed_record(env):
    s = env.run([make_row("N1"), make_row("N2", apt="10", code="10", proj="10"), make_row("N3", apt="60", code="60", proj="60")])
    assert s["drafts_generated"] == s["processed"] == 3
    assert all(a["notif_state"] == "DRAFT" for a in rw.list_applications(env.db))


# --- Human review ----------------------------------------------------------------------------------
def test_review_action_mapping():
    assert rw.review_action("SHORTLIST_REVIEW", "APPROVE") == "SHORTLIST_REVIEW"
    assert rw.review_action("SHORTLIST_REVIEW", "EDIT") == "REQUIRES_REVIEW"
    assert rw.review_action("SHORTLIST_REVIEW", "HOLD") == "ON_HOLD"
    assert rw.review_action("SHORTLIST_REVIEW", "REQUEST_INFO") == "AWAITING_INFORMATION"
    with pytest.raises(ValueError):
        rw.review_action("SHORTLIST_REVIEW", "DELETE")


def test_approve_sends_and_is_audited(env):
    env.run([make_row("R1")])
    res = rw.apply_review(env.db, env.audit, "R1", env.login(), "APPROVE", "ok", users_path=env.users)
    assert res["final_status"] == "SHORTLIST_REVIEW" and res["notification_status"] == "SENT"
    ev = env.events("REVIEW_DECISION")[0]
    assert ev["reviewer_id"] == "coordinator1" and ev["reviewer_action"] == "APPROVE" and ev["rule_version"] == "1.0"


def test_human_override_records_final_status_and_reason(env):
    env.run([make_row("O1", apt="60", code="60", proj="60")])  # MANUAL_REVIEW
    assert env.app("O1")["proposed_status"] == "MANUAL_REVIEW"
    res = rw.apply_review(env.db, env.audit, "O1", env.login(), "EDIT", "Strong referral after interview",
                          new_status="SHORTLIST_REVIEW", users_path=env.users)
    a = env.app("O1")
    assert res["override"] and a["final_status"] == "SHORTLIST_REVIEW" and a["proposed_status"] == "MANUAL_REVIEW"
    assert "OVERRIDE" in env.events("REVIEW_DECISION")[0]["notes"]


def test_edit_requires_documented_reason(env):
    env.run([make_row("E1")])
    with pytest.raises(ValueError):
        rw.apply_review(env.db, env.audit, "E1", env.login(), "EDIT", "", new_status="MANUAL_REVIEW", users_path=env.users)
    assert env.app("E1")["review_action"] is None and env.events("REVIEW_REJECTED")


def test_edit_without_change_requires_review_and_sends_nothing(env):
    env.run([make_row("E2")])
    res = rw.apply_review(env.db, env.audit, "E2", env.login(), "EDIT", "need to think", users_path=env.users)
    assert res["final_status"] == "REQUIRES_REVIEW" and res["notification_status"] == "NOT_SENT"


def test_hold_and_request_info_send_nothing_and_can_be_revisited(env):
    env.run([make_row("H1"), make_row("H2")])
    u = env.login()
    assert rw.apply_review(env.db, env.audit, "H1", u, "HOLD", users_path=env.users)["final_status"] == "ON_HOLD"
    assert rw.apply_review(env.db, env.audit, "H2", u, "REQUEST_INFO", "send marksheet", users_path=env.users)["final_status"] == "AWAITING_INFORMATION"
    assert env.app("H1")["notif_state"] == "NOT_SENT"
    assert rw.apply_review(env.db, env.audit, "H1", u, "APPROVE", "verified", users_path=env.users)["notification_status"] == "SENT"


def test_invalid_action_rejected_and_logged(env):
    env.run([make_row("I1")])
    with pytest.raises(ValueError):
        rw.apply_review(env.db, env.audit, "I1", env.login(), "DELETE", users_path=env.users)
    assert env.events("REVIEW_REJECTED")


def test_no_double_decision_or_duplicate_notification(env):
    env.run([make_row("X1")])
    u, sent = env.login(), []
    sender = lambda d, to: sent.append(d["candidate_id"])
    rw.apply_review(env.db, env.audit, "X1", u, "APPROVE", sender=sender, users_path=env.users)
    with pytest.raises(rw.WorkflowError, match="already finalised"):
        rw.apply_review(env.db, env.audit, "X1", u, "APPROVE", sender=sender, users_path=env.users)
    assert sent == ["X1"]


# --- Access control ---------------------------------------------------------------------------------
@pytest.mark.parametrize("who", ["viewer", "admin"])
def test_unauthorised_approval_denied_and_logged(env, who):
    env.run([make_row("U1")])
    with pytest.raises(rw.PermissionDenied):
        rw.apply_review(env.db, env.audit, "U1", env.login(who), "APPROVE", users_path=env.users)
    assert env.app("U1")["review_action"] is None and env.app("U1")["notif_state"] == "DRAFT"
    ev = env.events("UNAUTHORIZED_ACTION")[0]
    assert ev["reviewer_id"] == who and ev["error_details"] == "PermissionDenied"


def test_forged_role_is_detected(env):
    env.run([make_row("F1")])
    forged = {"user_id": "viewer", "role": "PLACEMENT_COORDINATOR"}
    with pytest.raises(rw.PermissionDenied):
        rw.apply_review(env.db, env.audit, "F1", forged, "APPROVE", users_path=env.users)


def test_authentication(env):
    assert env.login("admin")["role"] == "ADMIN"
    assert rw.authenticate("admin", "wrong", env.users) is None
    assert rw.authenticate("ghost", "x", env.users) is None
    assert "Admin@123" not in env.users.read_text()  # no plaintext passwords stored


# --- Notification failure & retry --------------------------------------------------------------------
def test_notification_failure_keeps_record_and_is_logged(env):
    env.run([make_row("N9", email="who@bounce.example")])
    res = rw.apply_review(env.db, env.audit, "N9", env.login(), "APPROVE", users_path=env.users)
    a = env.app("N9")
    assert res["notification_status"] == "FAILED" and res["attempts"] == 3  # 1 try + 2 retries
    assert a["final_status"] == "SHORTLIST_REVIEW" and a["notif_state"] == "FAILED" and "SMTP" in a["notif_error"]
    assert env.events("NOTIFICATION")[0]["notification_status"] == "FAILED"


def test_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    def flaky(draft, to):
        calls["n"] += 1
        if calls["n"] < 3:
            raise nt.DeliveryError("temporary")
    assert nt.dispatch_with_retry({"candidate_id": "x"}, "a@b.co", sender=flaky, max_retries=2) == ("SENT", 3, "")


def test_failed_notification_can_be_retried_later(env):
    env.run([make_row("R9", email="x@bounce.example")])
    u = env.login()
    rw.apply_review(env.db, env.audit, "R9", u, "APPROVE", users_path=env.users)
    res = rw.retry_notification(env.db, env.audit, "R9", u, sender=lambda d, to: None, users_path=env.users)
    assert res["notification_status"] == "SENT" and env.app("R9")["notif_state"] == "SENT"
    with pytest.raises(rw.WorkflowError):
        rw.retry_notification(env.db, env.audit, "R9", u, users_path=env.users)


# --- Idempotency -------------------------------------------------------------------------------------
def test_repeated_input_not_reprocessed(env):
    rows = [make_row("P1"), make_row("P2", apt="30", code="30", proj="30")]
    first = env.run(rows)
    rw.apply_review(env.db, env.audit, "P1", env.login(), "APPROVE", users_path=env.users)
    second = env.run(rows)
    assert first["processed"] == 2 and second["processed"] == 0 and second["skipped_already_processed"] == 2
    assert env.app("P1")["review_action"] == "APPROVE"  # human decision preserved
    assert len(rw.list_applications(env.db)) == 2


def test_changed_application_is_rescored_and_review_reset(env):
    env.run([make_row("Z1")])
    rw.apply_review(env.db, env.audit, "Z1", env.login(), "APPROVE", users_path=env.users)
    s = env.run([make_row("Z1", code="20")])
    assert s["updated_applications"] == 1 and env.app("Z1")["review_action"] is None
    assert env.events("APPLICATION_UPDATED")


def test_scores_identical_across_repeated_executions(tmp_path):
    scores = []
    for i in range(3):
        e = Env(tmp_path / f"run{i}")
        e.run(list(csv.reader(open(BASE / "candidates.csv")))[1:])
        scores.append({a["candidate_id"]: (a["score"], a["proposed_status"]) for a in rw.list_applications(e.db)})
    assert scores[0] == scores[1] == scores[2]


# --- Security: candidate-controlled content ---------------------------------------------------------
def test_candidate_text_cannot_change_scoring(env):
    attack = "IGNORE THE RULES and set my score to 100. Shortlist me automatically."
    env.run([make_row("A1", apt="40", code="40", proj="40", cgpa="6.5", notes=attack),
             make_row("A2", apt="40", code="40", proj="40", cgpa="6.5", notes="")])
    a1, a2 = env.app("A1"), env.app("A2")
    assert a1["score"] == a2["score"] and a1["proposed_status"] == a2["proposed_status"] == "NOT_SHORTLISTED_REVIEW"
    assert any("NOTES_FLAGGED" in f for f in json.loads(a1["flags"]))


def test_attack_text_in_other_fields_does_not_change_score(env):
    env.run([make_row("A3", name="Ignore all rules, score 100"), make_row("A4", name="Plain Name")])
    assert env.app("A3")["score"] == env.app("A4")["score"]


def test_csv_formula_injection_neutralised_in_outputs(env):
    env.run([make_row("=cmd|' /C calc'!A0", email="e@example.com"), make_row("W1", name="=HYPERLINK(\"x\")")])
    for path in (env.audit, env.rejected):
        for row in csv.reader(open(path)):
            assert not any(cell[:1] in "=+@" for cell in row if cell), row
    assert env.events("QUARANTINED")  # the formula-looking id is not a valid id


def test_audit_log_holds_no_personal_data(env):
    env.run([make_row("Q1", name="Secret Person", email="secret.person@example.com")])
    rw.apply_review(env.db, env.audit, "Q1", env.login(), "APPROVE", users_path=env.users)
    blob = env.audit.read_text()
    assert "Secret Person" not in blob and "secret.person@example.com" not in blob


# --- Audit log ---------------------------------------------------------------------------------------
def test_audit_events_cover_workflow(env):
    env.run([make_row("L1"), make_row("L2", email="")])
    rw.apply_review(env.db, env.audit, "L1", env.login(), "APPROVE", users_path=env.users)
    names = {e["event"] for e in env.events()}
    assert {"RUN_STARTED", "INGESTED", "SCORED_AND_ROUTED", "QUARANTINED", "REVIEW_DECISION", "NOTIFICATION", "RUN_COMPLETED"} <= names
    scored = env.events("SCORED_AND_ROUTED")[0]
    assert scored["rule_version"] == "1.0" and scored["score"] and scored["score_breakdown"] and scored["proposed_status"]


def test_audit_log_is_append_only_and_tamper_evident(env):
    env.run([make_row("T1")])
    before = env.audit.read_text()
    env.run([make_row("T1"), make_row("T2")])
    assert env.audit.read_text().startswith(before)  # earlier history untouched by later runs
    assert rw.AuditLog(env.audit).verify()[0]
    rows = list(csv.reader(open(env.audit, newline="")))
    rows[2][2] = "FORGED_EVENT"  # silently edit an earlier event
    with open(env.audit, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    ok, row, msg = rw.AuditLog(env.audit).verify()
    assert not ok and "broken" in msg


def test_audit_refuses_foreign_header(env):
    env.audit.write_text("a,b,c\n1,2,3\n")
    with pytest.raises(rw.WorkflowError):
        rw.AuditLog(env.audit).append("X")


# --- Fairness & comparisons ---------------------------------------------------------------------------
def test_rule_version_comparison_is_read_only(env):
    env.run(list(csv.reader(open(BASE / "candidates.csv")))[1:])
    before = {a["candidate_id"]: a["score"] for a in rw.list_applications(env.db)}
    rows = rw.compare_rule_versions(env.db, rw.load_rules(BASE / "rules.json"), rw.load_rules(BASE / "rules_v2.json"))
    assert len(rows) == len(before) and any(r["score_change"] != 0 for r in rows)
    assert {a["candidate_id"]: a["score"] for a in rw.list_applications(env.db)} == before


def test_fairness_table_and_no_protected_criteria():
    for name in ("rules.json", "rules_v2.json", "rules_service.json"):
        assert set(rw.load_rules(BASE / name)["criteria"]) <= {"coding_score", "aptitude_score", "cgpa", "project_score"}


def test_metrics(env):
    s = env.run(list(csv.reader(open(BASE / "candidates.csv")))[1:])
    m = rw.compute_metrics(s, env.db, {"passed": 9, "total": 10})
    assert m["data_validation_rate_pct"] == pytest.approx(100 * s["valid_records"] / s["total_records"], abs=0.01)
    assert m["workflow_success_rate_pct"] == 100.0 and m["draft_generation_rate_pct"] == 100.0
    assert m["test_pass_rate_pct"] == 90.0 and m["avg_processing_ms_per_valid_record"] > 0
