# Loan Decision Contract

## Overview
A Flask web application that simulates loan application decisions using a rules-based risk engine, with human review override support, persistent SQLite storage, and drift/stability monitoring (Decision Contract v0.3).

## Project Structure
- `main.py` - Single-file Flask app: DB setup, decision engine, drift monitoring, routes, templates
- `loan_sim.db` - SQLite database (auto-created, persistent across restarts)

## How It Works
The application uses a severity-based risk scoring system (Decision Contract v0.2):
- **Credit Score**: <580 high (0.35), 580-669 med (0.18), 670+ low (0.05)
- **DTI (loan/income)**: >0.6 high (0.30), 0.35-0.6 med (0.15), <0.35 low (0.05)
- **Employment Years**: <1 high (0.20), 1-2 med (0.10), 2+ low (0.03)
- Risk score = sum of impacts, clipped to [0,1]

### Decision Outcomes
- **APPROVE**: Risk score < 0.40
- **REVIEW**: Risk score 0.40-0.70 (sent to human review queue)
- **REJECT**: Risk score >= 0.70

### Explanation Filtering (Contract v0.2)
- REJECT: only high-severity reasons (max 4)
- REVIEW: high + med severity reasons (max 4)
- APPROVE: top 1-2 med+ reasons, or LOW_RISK if none qualify

### Drift & Stability Monitoring (Contract v0.3)
- `daily_metrics` table: aggregated per-day decision stats (counts, averages, reason_counts_json)
- `alerts` table: drift signals detected by comparing today vs 7-day trailing baseline
- Drift signals (all require >=10 decisions today):
  - **REVIEW_RATE_SPIKE**: today review rate > baseline + 10%
  - **RISK_SPIKE**: today avg risk > baseline + 0.15
  - **CREDIT_SHIFT**: abs credit score shift >= 40
  - **INCOME_SHIFT**: abs income shift >= 25%
- Metrics refresh triggered after: /apply, all simulation routes, human override

### Human Override
When a reviewer approves/rejects a REVIEW case, the decisions table is updated with the human outcome and engine_version set to "human-override-v1".

### Routes
- `GET /` - Redirects to /apply
- `GET /apply` - Loan application form
- `POST /apply` - Submit application
- `POST /simulate/today` - Generate 3 simulated applications per run (supports multiple runs per day)
- `GET /review` - Human review queue
- `POST /review/<id>/resolve` - Approve/reject a review task
- `GET /recent` - Recent decisions list
- `GET /decision/<id>` - Decision detail page (accepts decision_id or application_id)
- `GET /events` - Audit events log (HTML table, last 200 events)
- `GET /events.json` - Audit events log (JSON, last 200 events)
- `GET /alerts` - Alerts page (HTML, last 50 alerts)
- `GET /alerts.json` - Alerts (JSON, last 200 alerts)
- `GET /metrics/daily.json` - Daily metrics (JSON, last 30 days)
- `GET /sim` - Simulation page (generate random/borderline apps)
- `POST /sim/gen?count=N` - Generate N random synthetic applications
- `POST /sim/borderline` - Generate 1 borderline synthetic application
- `GET /dashboard` - Dashboard with decision metrics, reason codes, pending reviews, drift table, recent alerts
- `GET /dbinfo` - Debug info (JSON)

#### Governance JSON API (stable, for external dashboards)
- `GET /api/contract` - Contract metadata: version, engine, thresholds, schemas, definitions
- `GET /api/measure/summary?window=24h|7d|all` - Aggregated metrics with slips, corrections, drift snapshot
- `GET /api/events?limit=N` - Audit events (JSON, default 500, max 2000)
- `GET /api/decisions?limit=N` - Decisions with reasons (JSON, default 200, max 2000)
- `GET /api/alerts?limit=N` - Alerts (JSON, default 200, max 2000)

## Database
- Uses absolute path: `BASE_DIR/loan_sim.db`
- Single persistent DB file across all routes and restarts
- Tables: applications, decisions, explanations, review_tasks, simulation_runs, events, daily_metrics, alerts

## Running
```bash
python main.py
```
Server runs on port 5000 (or PORT env var).

## Deployment
Autoscale deployment using gunicorn: `gunicorn --bind=0.0.0.0:5000 --reuse-port main:APP`

## Recent Changes
- Feb 2026: Fixed persistent DB path, human override logic, decision detail page, added /dbinfo
- Feb 2026: Updated simulation_runs to support multiple runs per day with (sim_day, run_id) uniqueness. Naming pattern: sim-YYYY-MM-DD-R{run_id}-A/B/C. RNG seeded with "{sim_day}-R{run_id}" for unique results per run.
- Feb 2026: Added audit-grade events log. Events table tracks APPLICATION_SUBMITTED, AUTO_DECISION_MADE, SENT_TO_HUMAN_REVIEW, HUMAN_APPROVED, HUMAN_REJECTED with actor, metadata, and links to applications/decisions. Viewable at /events and /events.json.
- Feb 2026: Added /sim page for generating random and borderline synthetic applications. Added /dashboard with decision summary tables, top reason codes, and stuck review queue.
- Feb 2026: Implemented Decision Contract v0.2 — severity-based scoring with conditional explanation filtering. New thresholds: APPROVE<0.40, REVIEW 0.40-0.70, REJECT>=0.70. Reason codes now vary by decision outcome. Added decision_contract_version column to decisions table.
- Feb 2026: Implemented Decision Contract v0.3 — Drift & Stability monitoring. Added daily_metrics and alerts tables. compute_daily_metrics_and_alerts() aggregates per-day stats and detects drift signals (REVIEW_RATE_SPIKE, RISK_SPIKE, CREDIT_SHIFT, INCOME_SHIFT) vs 7-day trailing baseline. New routes: /metrics/daily.json, /alerts, /alerts.json. Dashboard extended with drift table and recent alerts. Alerts nav link added to all pages.
- Feb 2026: Added stable Governance JSON API (/api/*) for external dashboard consumption. Endpoints: /api/contract (metadata, thresholds, definitions), /api/measure/summary (windowed metrics with slips, corrections, drift snapshot), /api/events, /api/decisions, /api/alerts. Reuses existing helpers where possible. Existing HTML and .json routes preserved.
