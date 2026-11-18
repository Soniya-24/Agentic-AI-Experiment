# Lab 7 - Campus Placement Screening Workflow: Execution Report
_Generated 2026-10-28 08:15_ | rules `rules.json` v1.0 | run `RUN-20261028-081512-0cbf`

## Observations (Step 13)
| Parameter | Observation |
|---|---|
| Total candidate records | 120 |
| Valid records | 107 |
| Invalid or quarantined records | 13 |
| Duplicate records detected | 3 |
| Candidates in shortlist review | 24 |
| Candidates in manual review | 63 |
| Candidates in not-shortlisted review | 20 |
| Notification drafts generated | 107 |
| Human approvals / holds / edits / info requests | 30 / 16 / 3 / 2 |
| Reviewer overrides (status changed) | 3 |
| Notifications sent / failed / not sent | 31 / 2 / 18 |
| Total execution time | 0.4241 s |
| Test cases passed | 75 / 75 |

## Evaluation metrics
| Metric | Formula | Value |
|---|---|---|
| Data validation rate | valid / total x 100 | 89.17% |
| Workflow processing success rate | successfully processed valid / valid x 100 | 100.0% |
| Notification draft generation rate | drafts generated / drafts required x 100 | 100.0% |
| Test pass rate | passed / total x 100 | 100.0% |
| Average processing time | execution time / valid records processed | 3.964 ms per record |
| Human review completion rate | reviewed / requiring review x 100 | 30.84% |
| Notification delivery success rate | sent / (sent + failed) x 100 | 93.94% |

Audit log: 314 events, audit chain intact. Test run: `75 passed in 18.74s`.

## Fairness-oriented review (synthetic data)
| Branch | Applications | Shortlist review | Rate | Ratio to best | Below 4/5 rule |
|---|---|---|---|---|---|
| CS | 36 | 12 | 33.33% | 1.0 | no |
| IT | 27 | 9 | 33.33% | 1.0 | no |
| AIDS | 19 | 2 | 10.53% | 0.32 | YES |
| CIVIL | 12 | 0 | 0.0% | 0.0 | YES |
| MECH | 7 | 0 | 0.0% | 0.0 | YES |
| ENTC | 5 | 1 | 20.0% | 0.6 | YES |
| EXTC | 1 | 0 | 0.0% | 0.0 | no |

Branches outside the listed eligible branches (e.g. MECH, CIVIL) are routed to MANUAL_REVIEW by design, so their shortlist-review rate is zero until a human decides. This is a configuration choice that a reviewer should justify against the role requirements, not a validated fairness result. No gender, caste, religion, age, address or name field is used in scoring. These metrics measure software behaviour only; they do not prove that the criteria are fair.

## Rule version comparison (v1.0 vs v1.1)
6 of 107 applications change proposed status under v1.1.
