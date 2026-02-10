# Loan Decision Contract

## Overview
A Flask web application that simulates loan application decisions using a rules-based risk engine, with human review override support and persistent SQLite storage.

## Project Structure
- `main.py` - Single-file Flask app: DB setup, decision engine, routes, templates
- `loan_sim.db` - SQLite database (auto-created, persistent across restarts)

## How It Works
The application uses a weighted risk scoring system:
- **Credit Score** (55% weight): Lower score => higher risk
- **Debt-to-Income** (30% weight): Higher loan/income ratio => higher risk
- **Employment History** (15% weight): Shorter tenure => higher risk

### Decision Outcomes
- **APPROVE**: Risk score < 0.45
- **REVIEW**: Risk score 0.45-0.55 or edge cases (sent to human review queue)
- **REJECT**: Risk score > 0.55

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
- `GET /dbinfo` - Debug info (JSON)

## Database
- Uses absolute path: `BASE_DIR/loan_sim.db`
- Single persistent DB file across all routes and restarts
- Tables: applications, decisions, explanations, review_tasks, simulation_runs, events

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
