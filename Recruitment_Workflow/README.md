# Campus Placement Screening Workflow (Lab 7, HR Domain)

A rule-based screening pipeline for campus placement drives. It ingests student applications from CSV, validates and
quarantines bad data, scores each student with transparent weights from `rules.json`, routes applications to review
queues, drafts notifications, and requires a **placement coordinator's approval** before anything is "sent". Every step is
written to a tamper-evident audit log. A Streamlit dashboard provides the reviewer interface.

> The system only **proposes** a status. It never makes a final decision and never sends a real email (dispatch is simulated).
> All data is synthetic. Thresholds and weights are lab demonstrations, not recruitment standards.

## Setup

```bash
pip install streamlit pandas pytest        # Python 3.10+
python generate_dataset.py                 # optional: regenerates candidates.csv (120 synthetic records)
```

## Run

| Goal | Command |
|---|---|
| Dashboard (recommended) | `streamlit run app.py` |
| Console run (ingest, score, route, draft) | `python run_workflow.py` |
| Console run + simulated coordinator review | `python run_workflow.py --review auto` |
| Review in the terminal | `python run_workflow.py --review interactive` |
| Fresh run + lab report | `python run_workflow.py --reset --review auto --report` |
| Tests | `pytest -q` |

`--reset` archives the previous `workflow.db`, `audit_log.csv` and `rejected_records.csv` into `archive/` (nothing is deleted).
Running the pipeline twice on the same file is safe: unchanged applications are skipped.

### Demo logins (lab only)

| User | Password | Role | Can |
|---|---|---|---|
| `coordinator1`, `coordinator2` | `Placement@123`, `Placement@456` | Placement coordinator | review, approve, edit, hold, request info, view audit |
| `admin` | `Admin@123` | Administrator | run pipeline, view audit (cannot approve) |
| `viewer` | `Viewer@123` | Viewer | read-only |

Passwords are stored only as salted PBKDF2 hashes in `users.json`. Change them before any real use.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit dashboard: overview, review queue, run pipeline, insights, audit log |
| `run_workflow.py` | Console runner with the lab's expected output format |
| `recruitment_workflow.py` | Ingestion, validation, scoring, routing, review, RBAC, audit, metrics |
| `notifications.py` | Draft generation, message guards, simulated dispatch with retry |
| `candidates.csv` | 120 synthetic records incl. missing emails, duplicates, out-of-range values, boundary scores |
| `rules.json` | Weights, thresholds, eligibility, rule version (company A, product) |
| `rules_v2.json` | Variation: re-weighted rules (v1.1) for version comparison |
| `rules_service.json` | Second fictional company with different weights and eligibility |
| `audit_log.csv` | Append-only, hash-chained workflow history |
| `rejected_records.csv` | Quarantined records with reasons |
| `workflow.db` | SQLite workflow state (scores, proposals, decisions, notification state) |
| `test_recruitment.py` | 75 tests mapped to the lab's testing summary |
| `generate_report.py`, `lab_report.md` | Observation table and evaluation metrics |

## How it works

1. **Ingest**: reads CSV, checks headers, aborts before scoring if the file or rules are unreadable.
2. **Validate**: trims/normalises fields (e.g. `Computer` becomes `CS`), checks required fields, ranges and email format, quarantines invalid rows and flags duplicate IDs (never silently deleted).
3. **Score** (`rules.json`): `Score = 0.35 x Coding + 0.25 x Aptitude + 0.20 x (CGPA x 10) + 0.20 x Projects`, with a criterion-wise breakdown and the rule version recorded. Decimal arithmetic avoids float drift at thresholds.
4. **Route**: score >= 75 `SHORTLIST_REVIEW`, 50 to 74.99 `MANUAL_REVIEW`, below 50 `NOT_SHORTLISTED_REVIEW`. Failing hard eligibility (CGPA, backlogs) goes to not-shortlisted *review*; ambiguous or incomplete applications (branch not listed, missing project score) go to manual review.
5. **Draft**: a `DRAFT` email per status. Drafts never include scores and never state a final rejection.
6. **Review**: APPROVE, EDIT (status and/or wording, documented reason required), HOLD, REQUEST_INFO. Only coordinators can act; unauthorised attempts are denied and logged. Approved messages are sent through a simulated sender with up to 2 retries; failures are logged without losing the record.
7. **Audit**: every event is appended with a hash of the previous row. `AuditLog.verify()` (shown in the dashboard) detects edits to history. The log stores IDs, not names or emails.

## Security and fairness notes

- Candidate-supplied text (`candidate_notes`, names) can never change scores or rules; suspicious notes are flagged for the reviewer.
- CSV outputs neutralise formula injection (`=`, `+`, `-`, `@`).
- Scoring criteria are restricted to four documented, job-related inputs. Gender, caste, religion, age, address and name are not inputs.
- The Insights tab shows shortlist rates by branch with the four-fifths ratio. A gap is a prompt for humans to justify the criteria (branch eligibility is a configuration choice), not proof of fairness or bias.
- Metrics measure software behaviour. They do not validate that the criteria predict job performance.

## Lab variations implemented

Two rule versions compared (1), Streamlit dashboard (2), SQLite state (3), two company rule sets (4), notification retry, max 2 (6), status summary report (7), role-based access (9), fairness-oriented review (10).
