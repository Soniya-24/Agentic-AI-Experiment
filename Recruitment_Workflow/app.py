"""Streamlit dashboard for the Campus Placement Screening Workflow.

Run:  streamlit run app.py
Demo logins are listed in README.md (coordinator1, admin, viewer).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import recruitment_workflow as rw

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PLACEMENT_DATA_DIR", BASE))
DATA.mkdir(parents=True, exist_ok=True)
DB, AUDIT, REJECTED, USERS = DATA / "workflow.db", DATA / "audit_log.csv", DATA / "rejected_records.csv", DATA / "users.json"
UPLOADS = DATA / "uploads"

STATUS_LABEL = {
    rw.STATUS_SHORTLIST: "Shortlist review",
    rw.STATUS_MANUAL: "Manual review",
    rw.STATUS_NOT_SHORTLISTED: "Not-shortlisted review",
}

st.set_page_config(page_title="Placement Screening Workflow", page_icon=":mortar_board:", layout="wide")
rw.init_db(DB)
rw.ensure_default_users(USERS)


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
def flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    kind, message = st.session_state.pop("flash", (None, None))
    if kind:
        getattr(st, kind)(message)


def review_state(app: dict) -> str:
    if not app["review_action"]:
        return "Pending"
    if app["final_status"] == "ON_HOLD":
        return "On hold"
    if app["final_status"] == "AWAITING_INFORMATION":
        return "Awaiting info"
    if app["final_status"] == "REQUIRES_REVIEW":
        return "Needs re-review"
    return "Completed"


def apps_frame(apps: list[dict]) -> pd.DataFrame:
    rows = []
    for a in apps:
        flags = json.loads(a["flags"] or "[]")
        rows.append({
            "ID": a["candidate_id"], "Name": a["candidate_name"], "Branch": a["branch"], "Score": a["score"],
            "Proposed status": a["proposed_status"], "Flags": len(flags), "Review": review_state(a),
            "Final status": a["final_status"] or "", "Notification": a["notif_state"],
        })
    return pd.DataFrame(rows)


def rules_files() -> list[Path]:
    return sorted(BASE.glob("rules*.json"))


# ------------------------------------------------------------------------------------------------
# sidebar: login
# ------------------------------------------------------------------------------------------------
user = st.session_state.get("user")
with st.sidebar:
    st.title("Placement Screening")
    st.caption("Rule-based screening with human approval")
    if user:
        st.success(f"Signed in as **{user['user_id']}**\n\nRole: {user['role'].replace('_', ' ').title()}")
        if st.button("Sign out"):
            st.session_state.pop("user", None)
            st.rerun()
    else:
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in"):
                found = rw.authenticate(username, password, USERS)
                if found:
                    st.session_state["user"] = found
                    rw.AuditLog(AUDIT).append("LOGIN", reviewer_id=found["user_id"], reviewer_role=found["role"])
                    st.rerun()
                else:
                    rw.AuditLog(AUDIT).append("LOGIN_FAILED", reviewer_id=str(username)[:40], error_details="Invalid credentials")
                    st.error("Invalid username or password")
        st.caption("Demo accounts are listed in the README.")

if not user:
    st.header("Campus Placement Screening Workflow")
    st.write("Sign in from the sidebar to view applications. Recommendations are proposals only: "
             "a placement coordinator decides, and every action is written to an append-only audit log.")
    st.stop()

can_review = rw.authorize(user, "review")
can_run = rw.authorize(user, "run_pipeline")
can_audit = rw.authorize(user, "view_audit")

tab_names = ["Overview", "Review queue"]
if can_run:
    tab_names.append("Run pipeline")
tab_names.append("Insights")
if can_audit:
    tab_names.append("Audit log")
tabs = dict(zip(tab_names, st.tabs(tab_names)))
show_flash()

# ------------------------------------------------------------------------------------------------
# Overview
# ------------------------------------------------------------------------------------------------
with tabs["Overview"]:
    apps = rw.list_applications(DB)
    if not apps:
        st.info("No applications loaded yet." + (" Open **Run pipeline** to ingest candidates.csv." if can_run else
                                                  " Ask an administrator to run the pipeline."))
    else:
        sc, rc = rw.status_counts(DB), rw.review_counts(DB)
        c = st.columns(5)
        c[0].metric("Applications", rc["total_applications"])
        c[1].metric("Shortlist review", sc[rw.STATUS_SHORTLIST])
        c[2].metric("Manual review", sc[rw.STATUS_MANUAL])
        c[3].metric("Not-shortlisted review", sc[rw.STATUS_NOT_SHORTLISTED])
        c[4].metric("Reviews pending", rc["pending"])
        c = st.columns(5)
        c[0].metric("Decisions completed", rc["completed"])
        c[1].metric("On hold / info requested", rc["held"] + rc["info_requested"])
        c[2].metric("Overrides", rc["overrides"])
        c[3].metric("Notifications sent", rc["notifications_sent"])
        c[4].metric("Notifications failed", rc["notifications_failed"])

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Proposed status")
            st.bar_chart(pd.Series({STATUS_LABEL[k]: v for k, v in sc.items()}, name="Applications"))
        with right:
            st.subheader("Score distribution")
            scores = pd.Series([a["score"] for a in apps])
            bins = pd.cut(scores, bins=[0, 25, 50, 75, 100], include_lowest=True,
                          labels=["0-25", "25-50", "50-75", "75-100"]).value_counts().sort_index()
            st.bar_chart(bins)

        with st.expander("Latest run and evaluation metrics", expanded=False):
            with rw.db_conn(DB) as conn:
                row = conn.execute("SELECT summary FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            if row:
                summary = json.loads(row["summary"])
                m = rw.compute_metrics(summary, DB)
                st.json({"run": summary, "metrics": m})
        st.caption("These figures describe software and workflow behaviour. They do not show that the scoring criteria are fair or valid.")

# ------------------------------------------------------------------------------------------------
# Review queue
# ------------------------------------------------------------------------------------------------
with tabs["Review queue"]:
    apps = rw.list_applications(DB)
    if not apps:
        st.info("Nothing to review yet.")
    else:
        f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
        status_filter = f1.multiselect("Proposed status", list(STATUS_LABEL), default=list(STATUS_LABEL),
                                       format_func=STATUS_LABEL.get)
        review_filter = f2.multiselect("Review state", ["Pending", "On hold", "Awaiting info", "Needs re-review", "Completed"],
                                       default=["Pending", "On hold", "Awaiting info", "Needs re-review"])
        search = f3.text_input("Search ID or name")
        flagged_only = f4.checkbox("Flagged only")

        def keep(a):
            if a["proposed_status"] not in status_filter or review_state(a) not in review_filter:
                return False
            if flagged_only and not json.loads(a["flags"] or "[]"):
                return False
            return not search or search.lower() in (a["candidate_id"] + " " + a["candidate_name"]).lower()

        shown = [a for a in apps if keep(a)]
        st.caption(f"{len(shown)} of {len(apps)} applications")
        if shown:
            st.dataframe(apps_frame(shown), hide_index=True, height=260, width="stretch")

        if can_review:
            clean = [a for a in shown if a["proposed_status"] == rw.STATUS_SHORTLIST and not a["review_action"]
                     and not json.loads(a["flags"] or "[]")]
            with st.expander(f"Bulk approve clean shortlist-review applications ({len(clean)})"):
                st.write("Applies **APPROVE** individually to every pending, unflagged shortlist-review application in the "
                         "current filter. Each decision is logged separately and the approved draft is sent (simulated).")
                confirm = st.checkbox("I have reviewed these applications", key="bulk_confirm")
                if st.button("Approve selected", disabled=not (confirm and clean)):
                    ok = bad = 0
                    for a in clean:
                        try:
                            rw.apply_review(DB, AUDIT, a["candidate_id"], user, "APPROVE", "Bulk approval of clean shortlist",
                                            users_path=USERS)
                            ok += 1
                        except rw.WorkflowError:
                            bad += 1
                    flash("success", f"Approved {ok} application(s)" + (f"; {bad} could not be processed" if bad else ""))
                    st.rerun()

        st.divider()
        if not shown:
            st.info("No applications match the current filters.")
        else:
            options = [a["candidate_id"] for a in shown]
            cid = st.selectbox("Open application", options,
                               format_func=lambda i: next(f"{a['candidate_id']} - {a['candidate_name']}" for a in shown if a["candidate_id"] == i))
            app = rw.get_application(DB, cid)
            breakdown = json.loads(app["score_breakdown"])
            flags = json.loads(app["flags"] or "[]")

            d1, d2 = st.columns([2, 3])
            with d1:
                st.subheader(f"{app['candidate_id']}  |  {app['candidate_name']}")
                st.metric("Overall score", f"{app['score']:g} / 100")
                st.write(f"**Proposed status:** {app['proposed_status']}")
                st.write(f"**Rule version:** {app['rule_version']} (`{app['rules_file']}`)")
                st.write(f"**Branch:** {app['branch']}  |  **CGPA:** {app['cgpa']:g}  |  **Active backlogs:** {app['active_backlogs']}")
                for f in flags:
                    st.warning(f)
                if app["candidate_notes"]:
                    st.caption("Applicant note (never used for scoring):")
                    st.code(app["candidate_notes"], language=None)
            with d2:
                st.write("**Score explanation**")
                st.dataframe(pd.DataFrame([{
                    "Criterion": b["label"], "Input (0-100)": b["input"], "Weight %": b["weight"],
                    "Contribution": b["contribution"], "Max": b["max"], "Note": b["note"],
                } for b in breakdown]), hide_index=True, width="stretch")
                st.write(f"**Notification: {app['notif_state']}**  |  subject: {app['notif_subject']}")
                st.text_area("Draft message", app["notif_body"], height=210, disabled=True, key=f"view_{cid}_{app['notif_state']}")

            st.write(f"**Review state:** {review_state(app)}"
                     + (f"  |  decided by {app['reviewer_id']} ({app['review_action']}) - {app['review_notes']}" if app["review_action"] else ""))
            if app["notif_state"] == "FAILED":
                st.error(f"Notification failed after {app['notif_attempts']} attempt(s): {app['notif_error']}")
                if can_review and st.button("Retry notification", key=f"retry_{cid}"):
                    res = rw.retry_notification(DB, AUDIT, cid, user, users_path=USERS)
                    flash("success" if res["notification_status"] == "SENT" else "error",
                          f"Retry result: {res['notification_status']}")
                    st.rerun()

            finalised = bool(app["review_action"]) and app["final_status"] in rw.ROUTING_STATUSES
            if not can_review:
                st.info("Your role has read-only access. Only a placement coordinator can approve or change decisions.")
            elif finalised:
                st.success(f"Decision recorded: {app['final_status']}. No further action is available for this application.")
            else:
                st.subheader("Reviewer action")
                action = st.radio("Action", list(rw.REVIEW_ACTIONS), horizontal=True, key=f"act_{cid}",
                                  help="APPROVE sends the draft. EDIT can change the status and/or wording. "
                                       "HOLD and REQUEST_INFO do not send anything.")
                with st.form(f"review_{cid}"):
                    new_status, edited = None, None
                    if action == "EDIT":
                        new_status = st.selectbox("Status", list(rw.ROUTING_STATUSES),
                                                  index=list(rw.ROUTING_STATUSES).index(app["proposed_status"]))
                        edited = st.text_area("Message (leave unchanged to use the standard draft for the chosen status)",
                                              app["notif_body"], height=200)
                    notes = st.text_input("Reason / notes" + (" (required)" if action in ("EDIT", "REQUEST_INFO") else ""))
                    if st.form_submit_button(f"Submit {action}", type="primary"):
                        try:
                            res = rw.apply_review(
                                DB, AUDIT, cid, user, action, notes,
                                new_status=new_status if new_status != app["proposed_status"] or (edited or "").strip() != app["notif_body"].strip() else None,
                                edited_message=edited if edited and edited.strip() != app["notif_body"].strip() else None,
                                users_path=USERS)
                            msg = f"{cid}: {res['final_status']}" + (" (override)" if res["override"] else "")
                            if res["notification_status"] == "FAILED":
                                flash("error", f"{msg} - decision saved, but the notification failed: {res['error']}")
                            else:
                                flash("success", f"{msg} - notification {res['notification_status']}")
                            st.rerun()
                        except rw.PermissionDenied as exc:
                            st.error(str(exc))
                        except (rw.WorkflowError, ValueError) as exc:
                            st.error(str(exc))

# ------------------------------------------------------------------------------------------------
# Run pipeline (admin)
# ------------------------------------------------------------------------------------------------
if can_run:
    with tabs["Run pipeline"]:
        st.write("Ingests a CSV, validates and quarantines bad rows, scores with the chosen rules, routes each valid "
                 "application and prepares a draft notification. **No decision is made and nothing is sent.** "
                 "Re-running on unchanged records is safe: they are skipped.")
        rf = rules_files()
        rule_choice = st.selectbox("Scoring rules", rf, format_func=lambda p: p.name)
        try:
            r = rw.load_rules(rule_choice)
            st.caption(f"{r.get('company', '')} - version {r['rule_version']} - weights "
                       + ", ".join(f"{rw.CRITERIA_LABELS[k]} {v}%" for k, v in r["criteria"].items())
                       + f" - thresholds {r['thresholds']['shortlist_review']}/{r['thresholds']['manual_review']}")
        except rw.RulesError as exc:
            st.error(f"Rules problem: {exc}")
        source = st.radio("Candidate file", ["Bundled candidates.csv", "Upload a CSV"], horizontal=True)
        upload = st.file_uploader("CSV file", type=["csv"]) if source == "Upload a CSV" else None
        if st.button("Run pipeline", type="primary"):
            path = BASE / "candidates.csv"
            if source == "Upload a CSV":
                if upload is None:
                    st.error("Choose a CSV file first.")
                    path = None
                else:
                    UPLOADS.mkdir(exist_ok=True)
                    path = UPLOADS / f"upload_{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9_.-]', '_', upload.name)[:60]}"
                    path.write_bytes(upload.getvalue())
            if path is not None:
                try:
                    with st.spinner("Processing..."):
                        summary = rw.run_pipeline(path, rule_choice, DB, AUDIT, REJECTED, actor=user["user_id"])
                    st.session_state["last_summary"] = summary
                    st.success(f"Processed {summary['processed']} new application(s); {summary['skipped_already_processed']} "
                               f"already processed; {summary['invalid_records']} quarantined.")
                except rw.WorkflowError as exc:
                    st.error(f"Run aborted before scoring: {exc}")
        if "last_summary" in st.session_state:
            st.json(st.session_state["last_summary"])
        if REJECTED.is_file() and REJECTED.stat().st_size:
            st.subheader("Quarantined records")
            rej = pd.read_csv(REJECTED, dtype=str).fillna("")
            st.dataframe(rej[["row_number", "candidate_id", "category", "reason", "run_id"]], hide_index=True, width="stretch")

# ------------------------------------------------------------------------------------------------
# Insights
# ------------------------------------------------------------------------------------------------
with tabs["Insights"]:
    apps = rw.list_applications(DB)
    if not apps:
        st.info("Run the pipeline first.")
    else:
        st.subheader("Fairness-oriented review")
        fair = pd.DataFrame(rw.fairness_by_branch(DB))
        fair = fair.rename(columns={"branch": "Branch", "applications": "Applications", "shortlist_review": "Shortlist review",
                                    "shortlist_rate_pct": "Rate %", "ratio_to_best": "Ratio to best", "below_four_fifths": "Below 4/5 rule"})
        st.dataframe(fair, hide_index=True, width="stretch")
        st.bar_chart(fair.set_index("Branch")["Rate %"])
        st.caption("Branch is an eligibility input, and branches outside the eligible list are routed to manual review by design. "
                   "A gap here is a prompt for a human to justify the criteria, not proof of fairness or unfairness. "
                   "Gender, caste, religion, age, address and name are never scoring inputs.")

        st.subheader("Compare two rule versions")
        rf = rules_files()
        c1, c2 = st.columns(2)
        a_path = c1.selectbox("Rules A", rf, format_func=lambda p: p.name, key="cmp_a")
        b_path = c2.selectbox("Rules B", rf, index=next((i for i, p in enumerate(rf) if p.name == "rules_v2.json"), 0), format_func=lambda p: p.name, key="cmp_b")
        try:
            ra, rb = rw.load_rules(a_path), rw.load_rules(b_path)
            cmp_df = pd.DataFrame(rw.compare_rule_versions(DB, ra, rb))
            changed = cmp_df[cmp_df["status_changed"]]
            m1, m2, m3 = st.columns(3)
            m1.metric("Applications re-scored", len(cmp_df))
            m2.metric("Proposed status changes", len(changed))
            m3.metric("Mean score change", f"{cmp_df['score_change'].mean():+.2f}")
            st.dataframe(changed if len(changed) else cmp_df.head(20), hide_index=True, width="stretch")
            st.caption("Read-only what-if: stored decisions and scores are not changed.")
        except rw.RulesError as exc:
            st.error(f"Rules problem: {exc}")

# ------------------------------------------------------------------------------------------------
# Audit log
# ------------------------------------------------------------------------------------------------
if can_audit:
    with tabs["Audit log"]:
        audit = rw.AuditLog(AUDIT)
        ok, n, msg = audit.verify()
        (st.success if ok else st.error)(f"{msg} ({n} events)")
        rows = audit.read()
        if rows:
            df = pd.DataFrame(rows).drop(columns=["prev_hash"])
            f1, f2 = st.columns(2)
            ev = f1.multiselect("Event", sorted(df["event"].unique()))
            who = f2.text_input("Candidate ID contains")
            if ev:
                df = df[df["event"].isin(ev)]
            if who:
                df = df[df["candidate_id"].str.contains(who, case=False, na=False)]
            st.dataframe(df.iloc[::-1], hide_index=True, height=420, width="stretch")
            st.download_button("Download audit_log.csv", AUDIT.read_bytes(), "audit_log.csv", "text/csv")
        else:
            st.info("The audit log is empty.")
