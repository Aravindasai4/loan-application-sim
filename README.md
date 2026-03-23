# Loan Application Decision Engine

A rules-based automated loan decision engine that simulates a production 
credit decisioning system — built as the data source for the 
[AI Governance Dashboard](https://github.com/Aravindasai4/ai-governance-dashboard).

## What It Does

Simulates a real-time loan decisioning pipeline with:
- Automated APPROVE / REVIEW / REJECT decisions with risk scoring
- Three applicant archetypes (low/borderline/high risk) for realistic distributions
- Full event sourcing and audit trail
- Human review workflow with override tracking
- Versioned decision contracts (v0.2)
- Real-time metrics API consumed by the governance dashboard

## Architecture
```
Loan Application → Risk Engine → Decision + Reason Codes
                                      ↓
                              Event Log + Audit Trail
                                      ↓
                         AI Governance Dashboard (monitoring)
```

## Decision Thresholds

| Risk Score | Decision | Human Required |
|---|---|---|
| < 0.40 | APPROVE | No |
| 0.40 – 0.70 | REVIEW | Yes |
| > 0.70 | REJECT | No |

## API Endpoints

| Endpoint | Description |
|---|---|
| `/api/decisions` | Recent decisions with risk scores and reason codes |
| `/api/events` | Full audit trail event log |
| `/api/alerts` | Governance alerts (drift, review spikes) |
| `/api/measure/summary` | Aggregated metrics (slips, corrections, drift) |
| `/api/contract` | Versioned decision contract schema |

## Tech Stack

Python, Flask, SQLite

## Related Project

**AI Governance Dashboard** — monitors this engine in real time:
https://github.com/Aravindasai4/ai-governance-dashboard
