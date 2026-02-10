from __future__ import annotations

import json
import logging
import os
import sqlite3
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

from flask import Flask, request, redirect, url_for, render_template_string, abort, jsonify

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

APP = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "loan_sim.db")
DECISION_CONTRACT_VERSION = "v0.2"
ENGINE_VERSION = "rules-v0.2"
REVIEW_THRESHOLD = 0.40
REJECT_THRESHOLD = 0.70


# -----------------------------
# DB helpers
# -----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                applicant_name TEXT,
                annual_income REAL NOT NULL,
                loan_amount REAL NOT NULL,
                credit_score INTEGER NOT NULL,
                employment_years REAL NOT NULL,
                raw_json TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'manual',
                sim_day TEXT
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                risk_score REAL NOT NULL,
                decision TEXT NOT NULL,
                FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS explanations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                reason_details_json TEXT NOT NULL,
                FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );

            CREATE TABLE IF NOT EXISTS review_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                resolved_at TEXT,
                human_outcome TEXT,
                human_notes TEXT,
                FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );

            CREATE TABLE IF NOT EXISTS simulation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sim_day TEXT NOT NULL,
                run_id INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                num_created INTEGER NOT NULL,
                UNIQUE(sim_day, run_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                application_id INTEGER,
                decision_id INTEGER,
                review_task_id INTEGER,
                engine_version TEXT,
                risk_score REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_application_id ON events(application_id);
            CREATE INDEX IF NOT EXISTS idx_events_decision_id ON events(decision_id);

            CREATE TABLE IF NOT EXISTS daily_metrics (
                day TEXT PRIMARY KEY,
                total INTEGER,
                approve INTEGER,
                review INTEGER,
                reject INTEGER,
                avg_risk REAL,
                max_risk REAL,
                review_rate REAL,
                avg_income REAL,
                avg_loan REAL,
                avg_credit REAL,
                avg_emp_years REAL,
                reason_counts_json TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                day TEXT,
                severity TEXT,
                alert_type TEXT,
                message TEXT,
                details_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_day ON alerts(day);
            CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);
            """
        )


def migrate_db() -> None:
    with db() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "sim_day" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN sim_day TEXT")

        dec_cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "decision_contract_version" not in dec_cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN decision_contract_version TEXT")

        sr_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='simulation_runs'"
        ).fetchone()

        needs_rebuild = False
        if sr_exists:
            schema_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='simulation_runs'"
            ).fetchone()[0]
            if "UNIQUE(sim_day, run_id)" not in schema_sql.replace(" ", ""):
                needs_rebuild = True
        
        if needs_rebuild:
            conn.executescript("""
                ALTER TABLE simulation_runs RENAME TO simulation_runs_old;
                CREATE TABLE simulation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sim_day TEXT NOT NULL,
                    run_id INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    num_created INTEGER NOT NULL,
                    UNIQUE(sim_day, run_id)
                );
                INSERT INTO simulation_runs (sim_day, run_id, created_at, num_created)
                    SELECT sim_day, COALESCE(run_id, 1), created_at, num_created FROM simulation_runs_old;
                DROP TABLE simulation_runs_old;
            """)
        elif not sr_exists:
            conn.execute("""
            CREATE TABLE simulation_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              sim_day TEXT NOT NULL,
              run_id INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              num_created INTEGER NOT NULL,
              UNIQUE(sim_day, run_id)
            )
            """)

        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_applicant_name "
                "ON applications(applicant_name) WHERE applicant_name IS NOT NULL AND source = 'simulated'"
            )
        except Exception:
            pass

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                application_id INTEGER,
                decision_id INTEGER,
                review_task_id INTEGER,
                engine_version TEXT,
                risk_score REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
            CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_application_id ON events(application_id);
            CREATE INDEX IF NOT EXISTS idx_events_decision_id ON events(decision_id);
        """)

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS daily_metrics (
                day TEXT PRIMARY KEY,
                total INTEGER,
                approve INTEGER,
                review INTEGER,
                reject INTEGER,
                avg_risk REAL,
                max_risk REAL,
                review_rate REAL,
                avg_income REAL,
                avg_loan REAL,
                avg_credit REAL,
                avg_emp_years REAL,
                reason_counts_json TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                day TEXT,
                severity TEXT,
                alert_type TEXT,
                message TEXT,
                details_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alerts_day ON alerts(day);
            CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);
        """)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    actor: str,
    application_id: int | None = None,
    decision_id: int | None = None,
    review_task_id: int | None = None,
    engine_version: str | None = None,
    risk_score: float | None = None,
    metadata: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO events
          (created_at, event_type, actor, application_id, decision_id,
           review_task_id, engine_version, risk_score, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now_iso(),
            event_type,
            actor,
            application_id,
            decision_id,
            review_task_id,
            engine_version,
            risk_score,
            json.dumps(metadata or {}, default=str),
        ),
    )


# -----------------------------
# Drift & Stability (v0.3)
# -----------------------------
def compute_daily_metrics_and_alerts(conn: sqlite3.Connection, lookback_days: int = 30) -> None:
    try:
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

        rows = conn.execute(
            """
            SELECT
                DATE(d.created_at) AS day,
                COUNT(*) AS total,
                SUM(CASE WHEN d.decision = 'APPROVE' THEN 1 ELSE 0 END) AS approve,
                SUM(CASE WHEN d.decision = 'REVIEW' THEN 1 ELSE 0 END) AS review,
                SUM(CASE WHEN d.decision = 'REJECT' THEN 1 ELSE 0 END) AS reject,
                AVG(d.risk_score) AS avg_risk,
                MAX(d.risk_score) AS max_risk,
                AVG(a.annual_income) AS avg_income,
                AVG(a.loan_amount) AS avg_loan,
                AVG(a.credit_score) AS avg_credit,
                AVG(a.employment_years) AS avg_emp_years
            FROM decisions d
            JOIN applications a ON a.id = d.application_id
            WHERE DATE(d.created_at) >= ?
            GROUP BY DATE(d.created_at)
            """,
            (cutoff,),
        ).fetchall()

        updated_at = utc_now_iso()

        for r in rows:
            day = r["day"]
            total = r["total"] or 0
            review_count = r["review"] or 0
            review_rate = review_count / total if total > 0 else 0.0

            reason_rows = conn.execute(
                """
                SELECT e.reason_details_json
                FROM explanations e
                JOIN decisions d ON d.id = e.decision_id
                WHERE DATE(d.created_at) = ?
                """,
                (day,),
            ).fetchall()

            reason_counts: Dict[str, int] = {}
            for rr in reason_rows:
                try:
                    details = json.loads(rr["reason_details_json"] or "[]")
                    for item in details:
                        if isinstance(item, dict) and "code" in item:
                            reason_counts[item["code"]] = reason_counts.get(item["code"], 0) + 1
                except Exception:
                    pass

            conn.execute(
                """
                INSERT INTO daily_metrics
                    (day, total, approve, review, reject, avg_risk, max_risk,
                     review_rate, avg_income, avg_loan, avg_credit, avg_emp_years,
                     reason_counts_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    total=excluded.total, approve=excluded.approve,
                    review=excluded.review, reject=excluded.reject,
                    avg_risk=excluded.avg_risk, max_risk=excluded.max_risk,
                    review_rate=excluded.review_rate,
                    avg_income=excluded.avg_income, avg_loan=excluded.avg_loan,
                    avg_credit=excluded.avg_credit, avg_emp_years=excluded.avg_emp_years,
                    reason_counts_json=excluded.reason_counts_json,
                    updated_at=excluded.updated_at
                """,
                (
                    day, total, r["approve"] or 0, review_count, r["reject"] or 0,
                    round(r["avg_risk"] or 0, 6), round(r["max_risk"] or 0, 6),
                    round(review_rate, 6),
                    round(r["avg_income"] or 0, 2), round(r["avg_loan"] or 0, 2),
                    round(r["avg_credit"] or 0, 2), round(r["avg_emp_years"] or 0, 2),
                    json.dumps(reason_counts), updated_at,
                ),
            )

        today_str = now.strftime("%Y-%m-%d")
        today_row = conn.execute(
            "SELECT * FROM daily_metrics WHERE day = ?", (today_str,)
        ).fetchone()

        if not today_row or (today_row["total"] or 0) < 10:
            return

        baseline_start = (now - timedelta(days=8)).strftime("%Y-%m-%d")
        baseline_end = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        baseline = conn.execute(
            """
            SELECT
                AVG(review_rate) AS review_rate,
                AVG(avg_risk) AS avg_risk,
                AVG(avg_credit) AS avg_credit,
                AVG(avg_income) AS avg_income,
                COUNT(*) AS n_days
            FROM daily_metrics
            WHERE day >= ? AND day <= ?
            """,
            (baseline_start, baseline_end),
        ).fetchone()

        if not baseline or (baseline["n_days"] or 0) == 0:
            return

        signals = []
        t_rr = today_row["review_rate"] or 0
        b_rr = baseline["review_rate"] or 0
        if t_rr > b_rr + 0.10:
            signals.append(("REVIEW_RATE_SPIKE", "WARN",
                f"Review rate {t_rr:.2%} vs baseline {b_rr:.2%} (+{(t_rr - b_rr):.2%})",
                {"today": round(t_rr, 4), "baseline": round(b_rr, 4)}))

        t_ar = today_row["avg_risk"] or 0
        b_ar = baseline["avg_risk"] or 0
        if t_ar > b_ar + 0.15:
            signals.append(("RISK_SPIKE", "WARN",
                f"Avg risk {t_ar:.4f} vs baseline {b_ar:.4f} (+{(t_ar - b_ar):.4f})",
                {"today": round(t_ar, 4), "baseline": round(b_ar, 4)}))

        t_ac = today_row["avg_credit"] or 0
        b_ac = baseline["avg_credit"] or 0
        if abs(t_ac - b_ac) >= 40:
            signals.append(("CREDIT_SHIFT", "WARN",
                f"Avg credit score {t_ac:.0f} vs baseline {b_ac:.0f} (shift {t_ac - b_ac:+.0f})",
                {"today": round(t_ac, 2), "baseline": round(b_ac, 2)}))

        t_ai = today_row["avg_income"] or 0
        b_ai = baseline["avg_income"] or 0
        if b_ai > 0 and abs(t_ai - b_ai) / b_ai >= 0.25:
            signals.append(("INCOME_SHIFT", "WARN",
                f"Avg income ${t_ai:,.0f} vs baseline ${b_ai:,.0f} ({((t_ai - b_ai) / b_ai):+.1%})",
                {"today": round(t_ai, 2), "baseline": round(b_ai, 2)}))

        now_iso = utc_now_iso()
        for alert_type, severity, message, details in signals:
            exists = conn.execute(
                "SELECT 1 FROM alerts WHERE day = ? AND alert_type = ?",
                (today_str, alert_type),
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO alerts (created_at, day, severity, alert_type, message, details_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (now_iso, today_str, severity, alert_type, message, json.dumps(details)),
                )

    except Exception:
        logging.error("compute_daily_metrics_and_alerts failed:\n%s", traceback.format_exc())


def trigger_metrics_refresh():
    try:
        with db() as conn:
            compute_daily_metrics_and_alerts(conn)
    except Exception:
        logging.error("trigger_metrics_refresh failed:\n%s", traceback.format_exc())


# -----------------------------
# Decision engine (rules-based v0)
# -----------------------------
@dataclass
class AppInput:
    applicant_name: str
    annual_income: float
    loan_amount: float
    credit_score: int
    employment_years: float


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


IMPACT_WEIGHTS = {
    "CREDIT_SCORE_LOW": {"high": 0.35, "med": 0.18, "low": 0.05},
    "DTI_HIGH": {"high": 0.30, "med": 0.15, "low": 0.05},
    "EMPLOYMENT_SHORT": {"high": 0.20, "med": 0.10, "low": 0.03},
}


def compute_risk_and_reasons(inp: AppInput) -> Tuple[float, List[Dict], str]:
    dti = inp.loan_amount / max(inp.annual_income, 1.0)

    if inp.credit_score < 580:
        credit_sev = "high"
    elif inp.credit_score <= 669:
        credit_sev = "med"
    else:
        credit_sev = "low"

    if dti > 0.6:
        dti_sev = "high"
    elif dti >= 0.35:
        dti_sev = "med"
    else:
        dti_sev = "low"

    if inp.employment_years < 1:
        emp_sev = "high"
    elif inp.employment_years < 2:
        emp_sev = "med"
    else:
        emp_sev = "low"

    features = [
        {
            "code": "CREDIT_SCORE_LOW",
            "text": "Credit score is below the preferred range.",
            "value": inp.credit_score,
            "severity": credit_sev,
            "impact": IMPACT_WEIGHTS["CREDIT_SCORE_LOW"][credit_sev],
        },
        {
            "code": "DTI_HIGH",
            "text": "Requested loan amount is high relative to annual income (DTI proxy).",
            "value": round(dti, 4),
            "severity": dti_sev,
            "impact": IMPACT_WEIGHTS["DTI_HIGH"][dti_sev],
        },
        {
            "code": "EMPLOYMENT_SHORT",
            "text": "Employment history is short, which increases repayment uncertainty.",
            "value": inp.employment_years,
            "severity": emp_sev,
            "impact": IMPACT_WEIGHTS["EMPLOYMENT_SHORT"][emp_sev],
        },
    ]

    risk_score = clamp(sum(f["impact"] for f in features), 0.0, 1.0)

    if risk_score >= REJECT_THRESHOLD:
        decision = "REJECT"
    elif risk_score >= REVIEW_THRESHOLD:
        decision = "REVIEW"
    else:
        decision = "APPROVE"

    if decision == "REJECT":
        filtered = [f for f in features if f["severity"] == "high"]
    elif decision == "REVIEW":
        filtered = [f for f in features if f["severity"] in ("high", "med")]
    else:
        filtered = [f for f in features if f["severity"] in ("high", "med")]
        if not filtered:
            filtered = [{"code": "LOW_RISK", "text": "All risk factors are within acceptable range.", "value": round(risk_score, 4), "impact": 0.0, "severity": "low"}]
        else:
            filtered = filtered[:2]

    filtered.sort(key=lambda d: d["impact"], reverse=True)
    details = [{"code": f["code"], "text": f["text"], "value": f["value"], "impact": round(f["impact"], 4)} for f in filtered[:4]]

    return risk_score, details, decision


def decide(risk_score: float, decision: str, inp: AppInput) -> Tuple[str, bool]:
    needs_review = decision == "REVIEW"
    return decision, needs_review


# -----------------------------
# HTML templates (inline)
# -----------------------------
APPLY_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Loan Simulator - Apply</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 820px; margin: 32px auto; padding: 0 16px; }
    .row { display: flex; gap: 12px; }
    .col { flex: 1; }
    label { display:block; margin: 10px 0 4px; font-weight: 600; }
    input { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 8px; }
    button { margin-top: 16px; padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; }
    .nav a { margin-right: 12px; }
    .hint { color:#444; font-size: 0.92rem; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Loan Application (Simulator)</h1>
  <p class="hint">This is a simulator. It logs decisions + explanations and routes edge cases to human review.</p>
  
  <form method="POST" action="/simulate/today" style="margin: 12px 0;">
    <button type="submit">Simulate Today (3 apps)</button>
  </form>
  
  <form method="POST" action="/apply">
    <label>Applicant Name (optional)</label>
    <input name="applicant_name" placeholder="e.g., Aru"/>

    <div class="row">
      <div class="col">
        <label>Annual Income (USD)</label>
        <input name="annual_income" type="number" step="0.01" required placeholder="e.g., 85000"/>
      </div>
      <div class="col">
        <label>Loan Amount Requested (USD)</label>
        <input name="loan_amount" type="number" step="0.01" required placeholder="e.g., 12000"/>
      </div>
    </div>

    <div class="row">
      <div class="col">
        <label>Credit Score</label>
        <input name="credit_score" type="number" min="300" max="850" required placeholder="e.g., 690"/>
      </div>
      <div class="col">
        <label>Employment Years</label>
        <input name="employment_years" type="number" step="0.1" min="0" required placeholder="e.g., 3.5"/>
      </div>
    </div>

    <button type="submit">Submit Application</button>
  </form>
</body>
</html>
"""

RESULT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Decision Result</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 900px; margin: 32px auto; padding: 0 16px; }
    .pill { display:inline-block; padding: 6px 10px; border-radius: 999px; font-weight: 700; }
    .APPROVE { background:#e7ffef; }
    .REJECT { background:#ffe7e7; }
    .REVIEW { background:#fff6db; }
    ul { line-height: 1.7; }
    code { background:#f6f6f6; padding: 2px 6px; border-radius: 6px; }
    .nav a { margin-right: 12px; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Decision</h1>
  <p>
    Outcome:
    <span class="pill {{decision}}">{{decision}}</span>
    &nbsp;| Risk score: <code>{{risk_score}}</code>
    &nbsp;| Engine: <code>{{engine_version}}</code>
  </p>

  <h2>Top Reasons</h2>
  {% if reasons %}
  <ul>
    {% for r in reasons %}
      <li><b>{{r["code"]}}</b>: {{r["text"]}} (value={{r["value"]}}, impact={{r["impact"]}})</li>
    {% endfor %}
  </ul>
  {% else %}
  <p>No significant risk factors identified.</p>
  {% endif %}

  {% if decision == "REVIEW" %}
    <p><b>Human review required.</b> This case has been added to the review queue.</p>
  {% endif %}
</body>
</html>
"""

REVIEW_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Review Queue</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 10px; text-align: left; vertical-align: top; }
    .nav a { margin-right: 12px; }
    .btn { padding: 6px 10px; border-radius: 10px; border: 0; cursor:pointer; }
    .a { background:#e7ffef; }
    .r { background:#ffe7e7; }
    textarea { width: 100%; border: 1px solid #ddd; border-radius: 8px; padding: 8px; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Human Review Queue</h1>

  {% if tasks|length == 0 %}
    <p>No pending tasks.</p>
  {% else %}
    <table>
      <thead>
        <tr>
          <th>Task</th>
          <th>Applicant</th>
          <th>Inputs</th>
          <th>AI Result</th>
          <th>Top Reasons</th>
          <th>Resolve</th>
        </tr>
      </thead>
      <tbody>
        {% for t in tasks %}
          <tr>
            <td>#{{t["task_id"]}}<br><small>{{t["created_at"]}}</small></td>
            <td>{{t["applicant_name"] or ""}}</td>
            <td>
              income={{t["annual_income"]}}<br>
              loan={{t["loan_amount"]}}<br>
              score={{t["credit_score"]}}<br>
              empY={{t["employment_years"]}}
            </td>
            <td>
              decision=<b>{{t["decision"]}}</b><br>
              risk=<code>{{t["risk_score"]}}</code>
            </td>
            <td>
              {% for r in t["reasons"] %}
                <div><b>{{r["code"]}}</b> (impact {{r["impact"]}})</div>
              {% endfor %}
            </td>
            <td>
              <form method="POST" action="/review/{{t['task_id']}}/resolve">
                <textarea name="notes" rows="3" placeholder="optional notes..."></textarea>
                <input type="hidden" name="outcome" value="APPROVE">
                <button class="btn a" type="submit">Approve</button>
              </form>
              <form method="POST" action="/review/{{t['task_id']}}/resolve" style="margin-top:6px;">
                <textarea name="notes" rows="3" placeholder="optional notes..."></textarea>
                <input type="hidden" name="outcome" value="REJECT">
                <button class="btn r" type="submit">Reject</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}
</body>
</html>
"""

RECENT_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Recent Decisions</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 10px; text-align: left; }
    .nav a { margin-right: 12px; }
    code { background:#f6f6f6; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Recent Decisions</h1>
  <table>
    <thead>
      <tr>
        <th>Time</th><th>Applicant</th><th>Decision</th><th>Risk</th><th>Engine</th>
      </tr>
    </thead>
    <tbody>
      {% for r in rows %}
        <tr>
          <td>{{r["created_at"]}}</td>
          <td>
            <a href="/decision/{{r['decision_id']}}">
              {{r["applicant_name"] or "\u2014"}}
            </a>
          </td>
          <td><b>{{r["decision"]}}</b></td>
          <td><code>{{r["risk_score"]}}</code></td>
          <td><code>{{r["engine_version"]}}</code></td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""

DECISION_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Decision {{decision_id}}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 900px; margin: 32px auto; padding: 0 16px; }
    .nav a { margin-right: 12px; }
    .pill { display:inline-block; padding: 2px 8px; border-radius: 999px; background:#eee; margin-left: 8px;}
    pre { background:#f6f6f6; padding:12px; border-radius:10px; overflow:auto; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Decision</h1>

  <p>
    Applicant: <b>{{applicant_name or "\u2014"}}</b>
    <span class="pill">{{decision}}</span>
    <span class="pill">risk={{risk_score}}</span>
    <span class="pill">{{engine_version}}{% if contract_version %} | contract {{contract_version}}{% endif %}</span>
  </p>

  <h2>Inputs</h2>
  <ul>
    <li>Annual income: {{annual_income}}</li>
    <li>Loan amount: {{loan_amount}}</li>
    <li>Credit score: {{credit_score}}</li>
    <li>Employment years: {{employment_years}}</li>
    <li>Source: {{app_source}}</li>
    <li>Sim day: {{sim_day or "\u2014"}}</li>
  </ul>

  <h2>Top reasons</h2>
  {% if reasons %}
  <ul>
  {% for r in reasons %}
    <li><b>{{r.code}}</b>: {{r.text}} (value={{r.value}}, impact={{r.impact}})</li>
  {% endfor %}
  </ul>
  {% else %}
  <p>No significant risk factors identified.</p>
  {% endif %}

  <h3>Raw explanation JSON</h3>
  <pre>{{reason_details_json}}</pre>
</body>
</html>
"""


EVENTS_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Events Log</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1200px; margin: 32px auto; padding: 0 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align: left; font-size: 0.92rem; }
    .nav a { margin-right: 12px; }
    code { background:#f6f6f6; padding: 2px 6px; border-radius: 6px; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Events Log</h1>
  <p><a href="/events.json">JSON</a></p>
  <table>
    <thead>
      <tr>
        <th>Time</th><th>Type</th><th>Actor</th><th>Applicant</th><th>Decision</th><th>Risk</th><th>Engine</th><th>Links</th>
      </tr>
    </thead>
    <tbody>
      {% for e in events %}
        <tr>
          <td><small>{{e["created_at"]}}</small></td>
          <td><code>{{e["event_type"]}}</code></td>
          <td>{{e["actor"]}}</td>
          <td>{{e["applicant_name"] or ""}}</td>
          <td>{{e["decision"] or ""}}</td>
          <td>{% if e["risk_score"] is not none %}<code>{{e["risk_score"]}}</code>{% endif %}</td>
          <td>{% if e["engine_version"] %}<code>{{e["engine_version"]}}</code>{% endif %}</td>
          <td>{% if e["decision_id"] %}<a href="/decision/{{e['decision_id']}}">decision</a>{% endif %}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
</body>
</html>
"""


DASHBOARD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 16px; }
    .nav a { margin-right: 12px; }
    table { border-collapse: collapse; margin-bottom: 24px; }
    th, td { border-bottom: 1px solid #eee; padding: 8px 14px; text-align: left; }
    th { background: #fafafa; }
    h2 { margin-top: 32px; }
    code { background:#f6f6f6; padding: 2px 6px; border-radius: 6px; }
    .warn { color: #b45309; font-weight: 600; }
    .pill { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; }
    .INFO { background:#e0f0ff; color:#1a5276; }
    .WARN { background:#fff3cd; color:#856404; }
    .CRITICAL { background:#f8d7da; color:#721c24; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Dashboard</h1>

  <h2>Decision Summary</h2>
  <table>
    <thead>
      <tr>
        <th>Window</th><th>Total</th><th>Approve</th><th>Review</th><th>Reject</th>
        <th>Review Rate</th><th>Avg Risk</th><th>Max Risk</th><th>Human Overrides</th>
      </tr>
    </thead>
    <tbody>
      {% for w in windows %}
      <tr>
        <td><b>{{w.label}}</b></td>
        <td>{{w.total}}</td>
        <td>{{w.approve}}</td>
        <td>{{w.review}}</td>
        <td>{{w.reject}}</td>
        <td><code>{{w.review_rate}}</code></td>
        <td><code>{{w.avg_risk}}</code></td>
        <td><code>{{w.max_risk}}</code></td>
        <td>{{w.human_overrides}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <h2>Top Reason Codes (last 7 days)</h2>
  {% if reason_codes|length == 0 %}
    <p>No decisions in last 7 days.</p>
  {% else %}
  <table>
    <thead><tr><th>Code</th><th>Count</th></tr></thead>
    <tbody>
      {% for rc in reason_codes %}
      <tr><td><code>{{rc.code}}</code></td><td>{{rc.count}}</td></tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2>Stuck Review Queue</h2>
  <p>
    Pending review tasks: <b{% if pending_reviews > 0 %} class="warn"{% endif %}>{{pending_reviews}}</b>
  </p>

  <h2>Drift (last 14 days)</h2>
  {% if drift_rows|length == 0 %}
    <p>No daily metrics available yet.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th>Day</th><th>Total</th><th>Review Rate</th><th>Avg Risk</th>
        <th>Avg Credit</th><th>Avg Income</th><th>Flags</th>
      </tr>
    </thead>
    <tbody>
      {% for dr in drift_rows %}
      <tr>
        <td>{{dr.day}}</td>
        <td>{{dr.total}}</td>
        <td><code>{{dr.review_rate}}</code></td>
        <td><code>{{dr.avg_risk}}</code></td>
        <td><code>{{dr.avg_credit}}</code></td>
        <td><code>{{dr.avg_income}}</code></td>
        <td>
          {% for f in dr.flags %}<span class="warn">{{f}}</span> {% endfor %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}

  <h2>Alerts (recent)</h2>
  {% if recent_alerts|length == 0 %}
    <p>No alerts.</p>
  {% else %}
  <table>
    <thead><tr><th>Time</th><th>Day</th><th>Severity</th><th>Type</th><th>Message</th></tr></thead>
    <tbody>
      {% for al in recent_alerts %}
      <tr>
        <td><small>{{al.created_at}}</small></td>
        <td>{{al.day}}</td>
        <td><span class="pill {{al.severity}}">{{al.severity}}</span></td>
        <td><code>{{al.alert_type}}</code></td>
        <td>{{al.message}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>
"""


ALERTS_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Alerts</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 1200px; margin: 32px auto; padding: 0 16px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #eee; padding: 8px; text-align: left; font-size: 0.92rem; }
    .nav a { margin-right: 12px; }
    code { background:#f6f6f6; padding: 2px 6px; border-radius: 6px; }
    .pill { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.85rem; font-weight: 600; }
    .INFO { background:#e0f0ff; color:#1a5276; }
    .WARN { background:#fff3cd; color:#856404; }
    .CRITICAL { background:#f8d7da; color:#721c24; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Alerts</h1>
  <p><a href="/alerts.json">JSON</a> | <a href="/dashboard">Dashboard</a></p>

  {% if alerts|length == 0 %}
    <p>No alerts recorded yet.</p>
  {% else %}
  <table>
    <thead>
      <tr>
        <th>Time</th><th>Day</th><th>Severity</th><th>Type</th><th>Message</th>
      </tr>
    </thead>
    <tbody>
      {% for a in alerts %}
      <tr>
        <td><small>{{a["created_at"]}}</small></td>
        <td>{{a["day"]}}</td>
        <td><span class="pill {{a['severity']}}">{{a["severity"]}}</span></td>
        <td><code>{{a["alert_type"]}}</code></td>
        <td>{{a["message"]}}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</body>
</html>
"""


SIM_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Simulation</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 820px; margin: 32px auto; padding: 0 16px; }
    .nav a { margin-right: 12px; }
    button { margin-top: 10px; padding: 10px 14px; border: 0; border-radius: 10px; cursor: pointer; }
    .hint { color:#444; font-size: 0.92rem; }
  </style>
</head>
<body>
  <div class="nav">
    <a href="/apply">Apply</a>
    <a href="/review">Review Queue</a>
    <a href="/recent">Recent Decisions</a>
    <a href="/events">Events</a>
    <a href="/alerts">Alerts</a>
    <a href="/sim">Simulation</a>
    <a href="/dashboard">Dashboard</a>
  </div>

  <h1>Simulation</h1>
  <p class="hint">Generate synthetic loan applications using the normal rules engine. No forced outcomes.</p>

  <form method="POST" action="/sim/gen?count=1" style="display:inline;">
    <button type="submit">Generate 1 (random)</button>
  </form>

  <form method="POST" action="/sim/gen?count=10" style="display:inline; margin-left: 8px;">
    <button type="submit">Generate 10 (random batch)</button>
  </form>

  <form method="POST" action="/sim/borderline" style="display:inline; margin-left: 8px;">
    <button type="submit">Generate 1 (borderline / likely review)</button>
  </form>
</body>
</html>
"""


# -----------------------------
# Routes
# -----------------------------


def create_application_and_decide(inp: AppInput, source: str = "manual", sim_day: str | None = None):
    risk_score, reason_details, decision_outcome = compute_risk_and_reasons(inp)
    decision, needs_review = decide(risk_score, decision_outcome, inp)

    created_at = utc_now_iso()
    raw = {
        "applicant_name": inp.applicant_name,
        "annual_income": inp.annual_income,
        "loan_amount": inp.loan_amount,
        "credit_score": inp.credit_score,
        "employment_years": inp.employment_years,
        "source": source,
        "sim_day": sim_day,
    }

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO applications
              (created_at, applicant_name, annual_income, loan_amount, credit_score,
               employment_years, raw_json, source, sim_day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, inp.applicant_name, inp.annual_income, inp.loan_amount,
             inp.credit_score, inp.employment_years, json.dumps(raw), source, sim_day),
        )
        app_id = cur.lastrowid

        log_event(conn, "APPLICATION_SUBMITTED",
                  actor="human" if source == "manual" else "system",
                  application_id=app_id,
                  metadata={
                      "applicant_name": inp.applicant_name,
                      "annual_income": inp.annual_income,
                      "loan_amount": inp.loan_amount,
                      "credit_score": inp.credit_score,
                      "employment_years": inp.employment_years,
                      "source": source,
                  })

        cur = conn.execute(
            """
            INSERT INTO decisions (application_id, created_at, engine_version, risk_score, decision, decision_contract_version)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (app_id, created_at, ENGINE_VERSION, float(round(risk_score, 4)), decision, DECISION_CONTRACT_VERSION),
        )
        decision_id = cur.lastrowid

        conn.execute(
            """
            INSERT INTO explanations (decision_id, created_at, reasons_json, reason_details_json)
            VALUES (?, ?, ?, ?)
            """,
            (decision_id, created_at,
             json.dumps([d["code"] for d in reason_details]),
             json.dumps(reason_details)),
        )

        log_event(conn, "AUTO_DECISION_MADE",
                  actor="system",
                  application_id=app_id,
                  decision_id=decision_id,
                  engine_version=ENGINE_VERSION,
                  risk_score=float(round(risk_score, 4)),
                  metadata={
                      "decision": decision,
                      "reason_codes": [d["code"] for d in reason_details],
                      "approve_threshold": REVIEW_THRESHOLD,
                      "review_threshold": REJECT_THRESHOLD,
                      "decision_contract_version": DECISION_CONTRACT_VERSION,
                  })

        review_task_id = None
        if needs_review:
            cur2 = conn.execute(
                "INSERT INTO review_tasks (decision_id, created_at, status) VALUES (?, ?, 'PENDING')",
                (decision_id, created_at),
            )
            review_task_id = cur2.lastrowid

            log_event(conn, "SENT_TO_HUMAN_REVIEW",
                      actor="system",
                      application_id=app_id,
                      decision_id=decision_id,
                      review_task_id=review_task_id,
                      engine_version=ENGINE_VERSION,
                      risk_score=float(round(risk_score, 4)),
                      metadata={
                          "reason": "risk_in_review_band" if 0.40 <= risk_score < 0.70 else "forced_review_rule",
                          "decision": decision,
                      })

    return decision, risk_score, reason_details


@APP.get("/")
def root():
    return redirect(url_for("apply_get"))


@APP.get("/apply")
def apply_get():
    return render_template_string(APPLY_HTML)


@APP.post("/apply")
def apply_post():
    name = (request.form.get("applicant_name") or "").strip()
    try:
        annual_income = float(request.form["annual_income"])
        loan_amount = float(request.form["loan_amount"])
        credit_score = int(request.form["credit_score"])
        employment_years = float(request.form["employment_years"])
    except Exception:
        abort(400, "Invalid input types.")

    inp = AppInput(
        applicant_name=name,
        annual_income=annual_income,
        loan_amount=loan_amount,
        credit_score=credit_score,
        employment_years=employment_years,
    )

    decision, risk_score, reason_details = create_application_and_decide(inp, source="manual")
    trigger_metrics_refresh()

    return render_template_string(
        RESULT_HTML,
        decision=decision,
        risk_score=round(risk_score, 4),
        reasons=reason_details,
        engine_version=ENGINE_VERSION,
    )


@APP.post("/simulate/today")
def simulate_today():
    import random
    from uuid import uuid4
    sim_day = datetime.now(timezone.utc).date().isoformat()

    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(run_id), 0) AS max_run FROM simulation_runs WHERE sim_day = ?",
            (sim_day,),
        ).fetchone()
        run_id = (row["max_run"] if row else 0) + 1

        conn.execute(
            "INSERT INTO simulation_runs (sim_day, run_id, created_at, num_created) VALUES (?, ?, ?, 3)",
            (sim_day, run_id, utc_now_iso()),
        )

    seed = f"{sim_day}-R{run_id}-{uuid4()}"
    rng = random.Random(seed)

    prefix = f"sim-{sim_day}-R{run_id}"
    samples = [
        AppInput(f"{prefix}-A", rng.randint(80000, 140000),
                 rng.randint(5000, 20000), rng.randint(720, 810),
                 round(rng.uniform(3, 10), 1)),
        AppInput(f"{prefix}-B", rng.randint(40000, 80000),
                 rng.randint(15000, 40000), rng.randint(560, 680),
                 round(rng.uniform(0.5, 3.5), 1)),
        AppInput(f"{prefix}-C", rng.randint(25000, 60000),
                 rng.randint(30000, 90000), rng.randint(420, 590),
                 round(rng.uniform(0, 2.0), 1)),
    ]

    for s in samples:
        create_application_and_decide(s, source="simulated", sim_day=sim_day)

    trigger_metrics_refresh()
    return redirect(url_for("recent"))


@APP.get("/sim")
def sim_page():
    return render_template_string(SIM_HTML)


@APP.post("/sim/gen")
def sim_gen():
    import random, string
    count = min(int(request.args.get("count", 1)), 50)
    rng = random.Random()
    today = datetime.now(timezone.utc).date().strftime("%Y%m%d")

    for _ in range(count):
        tag = "".join(rng.choices(string.ascii_lowercase + string.digits, k=5))
        name = f"sim-{today}-{tag}"
        inp = AppInput(
            applicant_name=name,
            annual_income=rng.randint(25000, 150000),
            loan_amount=rng.randint(5000, 120000),
            credit_score=rng.randint(450, 820),
            employment_years=round(rng.uniform(0.0, 15.0), 1),
        )
        create_application_and_decide(inp, source="sim")

    trigger_metrics_refresh()
    return redirect(url_for("recent"))


@APP.post("/sim/borderline")
def sim_borderline():
    import random, string
    rng = random.Random()
    today = datetime.now(timezone.utc).date().strftime("%Y%m%d")
    tag = "".join(rng.choices(string.ascii_lowercase + string.digits, k=5))
    name = f"sim-{today}-{tag}"

    annual_income = rng.randint(40000, 100000)
    dti_ratio = round(rng.uniform(0.35, 0.55), 2)
    loan_amount = int(annual_income * dti_ratio)

    inp = AppInput(
        applicant_name=name,
        annual_income=annual_income,
        loan_amount=loan_amount,
        credit_score=rng.randint(560, 660),
        employment_years=round(rng.uniform(0.2, 2.0), 1),
    )
    create_application_and_decide(inp, source="sim")

    trigger_metrics_refresh()
    return redirect(url_for("recent"))


@APP.get("/dashboard")
def dashboard():
    from collections import Counter

    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    windows = []
    with db() as conn:
        for label, cutoff in [("Last 24 hours", cutoff_24h), ("Last 7 days", cutoff_7d), ("All time", None)]:
            where = "WHERE d.created_at >= ?" if cutoff else ""
            params = (cutoff,) if cutoff else ()

            row = conn.execute(
                f"""
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN d.decision = 'APPROVE' THEN 1 ELSE 0 END) AS approve,
                  SUM(CASE WHEN d.decision = 'REVIEW' THEN 1 ELSE 0 END) AS review,
                  SUM(CASE WHEN d.decision = 'REJECT' THEN 1 ELSE 0 END) AS reject,
                  AVG(d.risk_score) AS avg_risk,
                  MAX(d.risk_score) AS max_risk
                FROM decisions d
                {where}
                """,
                params,
            ).fetchone()

            total = row["total"] or 0
            review_count = row["review"] or 0
            review_rate = round(review_count / total, 4) if total > 0 else 0.0

            override_where = "WHERE ev.event_type IN ('HUMAN_APPROVED', 'HUMAN_REJECTED')"
            if cutoff:
                override_where += " AND ev.created_at >= ?"
            override_row = conn.execute(
                f"SELECT COUNT(*) AS c FROM events ev {override_where}",
                params,
            ).fetchone()

            windows.append({
                "label": label,
                "total": total,
                "approve": row["approve"] or 0,
                "review": review_count,
                "reject": row["reject"] or 0,
                "review_rate": f"{review_rate:.2%}",
                "avg_risk": round(row["avg_risk"] or 0, 4),
                "max_risk": round(row["max_risk"] or 0, 4),
                "human_overrides": override_row["c"] or 0,
            })

        reason_rows = conn.execute(
            """
            SELECT e.reason_details_json
            FROM explanations e
            JOIN decisions d ON d.id = e.decision_id
            WHERE d.created_at >= ?
            """,
            (cutoff_7d,),
        ).fetchall()

        code_counter = Counter()
        for r in reason_rows:
            try:
                details = json.loads(r["reason_details_json"] or "[]")
                for item in details:
                    if "code" in item:
                        code_counter[item["code"]] += 1
            except Exception:
                pass

        reason_codes = [{"code": code, "count": count} for code, count in code_counter.most_common(10)]

        pending_row = conn.execute(
            "SELECT COUNT(*) AS c FROM review_tasks WHERE status = 'PENDING'"
        ).fetchone()
        pending_reviews = pending_row["c"] or 0

        cutoff_14d = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        drift_metric_rows = conn.execute(
            "SELECT * FROM daily_metrics WHERE day >= ? ORDER BY day DESC",
            (cutoff_14d,),
        ).fetchall()

        drift_rows = []
        for dm in drift_metric_rows:
            day = dm["day"]
            alert_types_rows = conn.execute(
                "SELECT alert_type FROM alerts WHERE day = ?", (day,)
            ).fetchall()
            alert_types = {r["alert_type"] for r in alert_types_rows}
            flags = []
            if "REVIEW_RATE_SPIKE" in alert_types:
                flags.append("review spike")
            if "RISK_SPIKE" in alert_types:
                flags.append("risk spike")
            if "CREDIT_SHIFT" in alert_types:
                flags.append("credit shift")
            if "INCOME_SHIFT" in alert_types:
                flags.append("income shift")
            drift_rows.append({
                "day": day,
                "total": dm["total"] or 0,
                "review_rate": f"{(dm['review_rate'] or 0):.2%}",
                "avg_risk": round(dm["avg_risk"] or 0, 4),
                "avg_credit": round(dm["avg_credit"] or 0, 0),
                "avg_income": f"${(dm['avg_income'] or 0):,.0f}",
                "flags": flags,
            })

        recent_alert_rows = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent_alerts = [dict(r) for r in recent_alert_rows]

    return render_template_string(
        DASHBOARD_HTML,
        windows=windows,
        reason_codes=reason_codes,
        pending_reviews=pending_reviews,
        drift_rows=drift_rows,
        recent_alerts=recent_alerts,
    )


@APP.get("/review")
def review_queue():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              rt.id AS task_id,
              rt.created_at AS created_at,
              a.applicant_name,
              a.annual_income, a.loan_amount, a.credit_score, a.employment_years,
              d.decision, d.risk_score,
              e.reason_details_json
            FROM review_tasks rt
            JOIN decisions d ON d.id = rt.decision_id
            JOIN applications a ON a.id = d.application_id
            LEFT JOIN explanations e ON e.decision_id = d.id
            WHERE rt.status = 'PENDING'
            ORDER BY rt.created_at DESC
            """
        ).fetchall()

    tasks = []
    for r in rows:
        try:
            reasons = json.loads(r["reason_details_json"] or "[]")
        except Exception:
            reasons = []
        tasks.append(
            {
                "task_id": r["task_id"],
                "created_at": r["created_at"],
                "applicant_name": r["applicant_name"],
                "annual_income": r["annual_income"],
                "loan_amount": r["loan_amount"],
                "credit_score": r["credit_score"],
                "employment_years": r["employment_years"],
                "decision": r["decision"],
                "risk_score": r["risk_score"],
                "reasons": reasons,
            }
        )

    return render_template_string(REVIEW_HTML, tasks=tasks)


@APP.post("/review/<int:task_id>/resolve")
def resolve_task(task_id: int):
    outcome = request.form.get("outcome", "").strip().upper()
    notes = (request.form.get("notes") or "").strip()

    if outcome not in ("APPROVE", "REJECT"):
        abort(400, "Outcome must be APPROVE or REJECT.")

    with db() as conn:
        row = conn.execute(
            "SELECT id, status, decision_id FROM review_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            abort(404, "Task not found.")
        if row["status"] != "PENDING":
            abort(400, "Task already resolved.")

        now = utc_now_iso()

        conn.execute(
            """
            UPDATE review_tasks
            SET status='RESOLVED', resolved_at=?, human_outcome=?, human_notes=?
            WHERE id=?
            """,
            (now, outcome, notes, task_id),
        )

        conn.execute(
            """
            UPDATE decisions
            SET decision=?, engine_version='human-override-v1'
            WHERE id=?
            """,
            (outcome, row["decision_id"]),
        )

        dec_row = conn.execute(
            "SELECT application_id FROM decisions WHERE id = ?", (row["decision_id"],)
        ).fetchone()
        app_id = dec_row["application_id"] if dec_row else None

        event_type = "HUMAN_APPROVED" if outcome == "APPROVE" else "HUMAN_REJECTED"
        log_event(conn, event_type,
                  actor="human",
                  application_id=app_id,
                  decision_id=row["decision_id"],
                  review_task_id=task_id,
                  engine_version="human-override-v1",
                  metadata={"notes": notes, "outcome": outcome})

    trigger_metrics_refresh()
    return redirect(url_for("review_queue"))


@APP.get("/recent")
def recent():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.id AS decision_id, d.created_at, a.applicant_name, d.decision, d.risk_score, d.engine_version
            FROM decisions d
            JOIN applications a ON a.id = d.application_id
            ORDER BY d.created_at DESC
            LIMIT 500
            """
        ).fetchall()
    return render_template_string(RECENT_HTML, rows=rows)


@APP.get("/decision/<int:lookup_id>")
def decision_page(lookup_id: int):
    with db() as conn:
        row = conn.execute("""
            SELECT
              d.id as decision_id,
              d.created_at,
              d.engine_version,
              d.risk_score,
              d.decision,
              d.decision_contract_version,
              a.applicant_name,
              a.annual_income,
              a.loan_amount,
              a.credit_score,
              a.employment_years,
              a.source,
              a.sim_day,
              e.reason_details_json
            FROM decisions d
            JOIN applications a ON a.id = d.application_id
            LEFT JOIN explanations e ON e.decision_id = d.id
            WHERE d.id = ?
            ORDER BY d.created_at DESC
            LIMIT 1
        """, (lookup_id,)).fetchone()

        if not row:
            row = conn.execute("""
                SELECT
                  d.id as decision_id,
                  d.created_at,
                  d.engine_version,
                  d.risk_score,
                  d.decision,
                  d.decision_contract_version,
                  a.applicant_name,
                  a.annual_income,
                  a.loan_amount,
                  a.credit_score,
                  a.employment_years,
                  a.source,
                  a.sim_day,
                  e.reason_details_json
                FROM decisions d
                JOIN applications a ON a.id = d.application_id
                LEFT JOIN explanations e ON e.decision_id = d.id
                WHERE d.application_id = ?
                ORDER BY d.created_at DESC
                LIMIT 1
            """, (lookup_id,)).fetchone()

    if not row:
        return ("Decision not found", 404)

    reasons = []
    raw_json = ""
    try:
        raw_json = row["reason_details_json"] or "[]"
        reasons = json.loads(raw_json)
        if not isinstance(reasons, list):
            reasons = []
    except Exception:
        reasons = []

    return render_template_string(
        DECISION_HTML,
        decision_id=row["decision_id"],
        applicant_name=row["applicant_name"],
        decision=row["decision"],
        risk_score=row["risk_score"],
        engine_version=row["engine_version"],
        contract_version=row["decision_contract_version"] or "",
        annual_income=row["annual_income"],
        loan_amount=row["loan_amount"],
        credit_score=row["credit_score"],
        employment_years=row["employment_years"],
        app_source=row["source"],
        sim_day=row["sim_day"],
        reasons=reasons,
        reason_details_json=raw_json,
    )


def _fetch_events(limit: int = 200):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
              ev.id, ev.created_at, ev.event_type, ev.actor,
              ev.application_id, ev.decision_id, ev.review_task_id,
              ev.engine_version, ev.risk_score, ev.metadata_json,
              a.applicant_name,
              d.decision
            FROM events ev
            LEFT JOIN applications a ON a.id = ev.application_id
            LEFT JOIN decisions d ON d.id = ev.decision_id
            ORDER BY ev.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@APP.get("/events")
def events_page():
    events = _fetch_events(200)
    return render_template_string(EVENTS_HTML, events=events)


@APP.get("/events.json")
def events_json():
    events = _fetch_events(200)
    return jsonify(events)


@APP.get("/metrics/daily.json")
def metrics_daily_json():
    with db() as conn:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM daily_metrics WHERE day >= ? ORDER BY day ASC",
            (cutoff,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["reason_counts"] = json.loads(d.pop("reason_counts_json", "{}") or "{}")
        except Exception:
            d["reason_counts"] = {}
        result.append(d)
    return jsonify(result)


@APP.get("/alerts")
def alerts_page():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    alerts = [dict(r) for r in rows]
    return render_template_string(ALERTS_HTML, alerts=alerts)


@APP.get("/alerts.json")
def alerts_json():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d.pop("details_json", "{}") or "{}")
        except Exception:
            d["details"] = {}
        result.append(d)
    return jsonify(result)


@APP.get("/dbinfo")
def dbinfo():
    with db() as conn:
        app_count = conn.execute("SELECT COUNT(*) as c FROM applications").fetchone()["c"]
        dec_count = conn.execute("SELECT COUNT(*) as c FROM decisions").fetchone()["c"]
        rev_count = conn.execute("SELECT COUNT(*) as c FROM review_tasks").fetchone()["c"]

    return jsonify({
        "db_path": os.path.abspath(DB_PATH),
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "applications_count": app_count,
        "decisions_count": dec_count,
        "review_tasks_count": rev_count,
    })


# -----------------------------
# Governance JSON API (/api/*)
# -----------------------------

ALL_EVENT_TYPES = [
    "APPLICATION_SUBMITTED",
    "AUTO_DECISION_MADE",
    "SENT_TO_HUMAN_REVIEW",
    "HUMAN_APPROVED",
    "HUMAN_REJECTED",
]

ALL_REASON_CODES = [
    "CREDIT_SCORE_LOW",
    "DTI_HIGH",
    "EMPLOYMENT_SHORT",
    "LOW_RISK",
]


def _window_cutoff(window: str) -> str | None:
    now = datetime.now(timezone.utc)
    if window == "24h":
        return (now - timedelta(hours=24)).isoformat()
    elif window == "7d":
        return (now - timedelta(days=7)).isoformat()
    return None


def _count_slips_and_corrections(conn: sqlite3.Connection, cutoff: str | None) -> Tuple[int, int]:
    where = ""
    params: tuple = ()
    if cutoff:
        where = "AND rt.resolved_at >= ?"
        params = (cutoff,)

    rows = conn.execute(
        f"""
        SELECT
            ev_auto.metadata_json AS auto_meta,
            rt.human_outcome,
            ev_auto.risk_score AS auto_risk
        FROM review_tasks rt
        JOIN decisions d ON d.id = rt.decision_id
        LEFT JOIN events ev_auto ON ev_auto.decision_id = d.id
            AND ev_auto.event_type = 'AUTO_DECISION_MADE'
        WHERE rt.status = 'RESOLVED'
          AND rt.human_outcome IS NOT NULL
          {where}
        """,
        params,
    ).fetchall()

    slips = 0
    corrections = 0
    for r in rows:
        human = r["human_outcome"]
        auto_decision = None

        try:
            auto_meta = json.loads(r["auto_meta"] or "{}")
            auto_decision = auto_meta.get("decision")
        except Exception:
            pass

        if not auto_decision and r["auto_risk"] is not None:
            rs = r["auto_risk"]
            if rs >= REJECT_THRESHOLD:
                auto_decision = "REJECT"
            elif rs >= REVIEW_THRESHOLD:
                auto_decision = "REVIEW"
            else:
                auto_decision = "APPROVE"

        if not auto_decision:
            auto_decision = "REVIEW"

        if auto_decision == "REVIEW":
            corrections += 1
        elif (auto_decision == "APPROVE" and human == "REJECT") or \
             (auto_decision == "REJECT" and human == "APPROVE"):
            slips += 1

    return slips, corrections


def _compute_summary(conn: sqlite3.Connection, window: str) -> dict:
    cutoff = _window_cutoff(window)
    where = "WHERE d.created_at >= ?" if cutoff else ""
    params: tuple = (cutoff,) if cutoff else ()

    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN d.decision = 'APPROVE' THEN 1 ELSE 0 END) AS approve,
          SUM(CASE WHEN d.decision = 'REVIEW' THEN 1 ELSE 0 END) AS review,
          SUM(CASE WHEN d.decision = 'REJECT' THEN 1 ELSE 0 END) AS reject,
          AVG(d.risk_score) AS avg_risk,
          MAX(d.risk_score) AS max_risk
        FROM decisions d
        {where}
        """,
        params,
    ).fetchone()

    total = row["total"] or 0
    review_count = row["review"] or 0
    review_rate = round(review_count / total, 6) if total > 0 else 0.0

    override_where = "WHERE ev.event_type IN ('HUMAN_APPROVED', 'HUMAN_REJECTED')"
    if cutoff:
        override_where += " AND ev.created_at >= ?"
    override_row = conn.execute(
        f"SELECT COUNT(*) AS c FROM events ev {override_where}",
        params,
    ).fetchone()

    slips, corrections = _count_slips_and_corrections(conn, cutoff)

    from collections import Counter
    reason_where = "WHERE d.created_at >= ?" if cutoff else ""
    reason_rows = conn.execute(
        f"""
        SELECT e.reason_details_json
        FROM explanations e
        JOIN decisions d ON d.id = e.decision_id
        {reason_where}
        """,
        params,
    ).fetchall()

    code_counter = Counter()
    for rr in reason_rows:
        try:
            details = json.loads(rr["reason_details_json"] or "[]")
            for item in details:
                if isinstance(item, dict) and "code" in item:
                    code_counter[item["code"]] += 1
        except Exception:
            pass

    top_reason_codes = [{"code": code, "count": count} for code, count in code_counter.most_common(10)]

    drift = _compute_drift_snapshot(conn, cutoff)

    return {
        "window": window,
        "total": total,
        "approve": row["approve"] or 0,
        "review": review_count,
        "reject": row["reject"] or 0,
        "review_rate": round(review_rate, 6),
        "avg_risk": round(row["avg_risk"] or 0, 6),
        "max_risk": round(row["max_risk"] or 0, 6),
        "human_overrides": override_row["c"] or 0,
        "slips": slips,
        "corrections": corrections,
        "top_reason_codes": top_reason_codes,
        "drift": drift,
    }


def _compute_drift_snapshot(conn: sqlite3.Connection, cutoff: str | None) -> dict:
    where = "WHERE d.created_at >= ?" if cutoff else ""
    params: tuple = (cutoff,) if cutoff else ()

    row = conn.execute(
        f"""
        SELECT
            AVG(a.credit_score) AS credit_avg,
            AVG(a.annual_income) AS income_avg,
            AVG(CASE WHEN a.annual_income > 0 THEN a.loan_amount / a.annual_income ELSE 0 END) AS dti_avg
        FROM decisions d
        JOIN applications a ON a.id = d.application_id
        {where}
        """,
        params,
    ).fetchone()

    now = datetime.now(timezone.utc)
    baseline_start = (now - timedelta(days=8)).strftime("%Y-%m-%d")
    baseline_end = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    baseline = conn.execute(
        """
        SELECT
            AVG(a.credit_score) AS credit_baseline,
            AVG(a.annual_income) AS income_baseline,
            AVG(CASE WHEN a.annual_income > 0 THEN a.loan_amount / a.annual_income ELSE 0 END) AS dti_baseline
        FROM decisions d
        JOIN applications a ON a.id = d.application_id
        WHERE DATE(d.created_at) >= ? AND DATE(d.created_at) <= ?
        """,
        (baseline_start, baseline_end),
    ).fetchone()

    credit_avg = round(row["credit_avg"] or 0, 2) if row["credit_avg"] else None
    income_avg = round(row["income_avg"] or 0, 2) if row["income_avg"] else None
    dti_avg = round(row["dti_avg"] or 0, 6) if row["dti_avg"] else None

    credit_baseline = round(baseline["credit_baseline"] or 0, 2) if baseline and baseline["credit_baseline"] else None
    income_baseline = round(baseline["income_baseline"] or 0, 2) if baseline and baseline["income_baseline"] else None
    dti_baseline = round(baseline["dti_baseline"] or 0, 6) if baseline and baseline["dti_baseline"] else None

    credit_shift = round(credit_avg - credit_baseline, 2) if credit_avg is not None and credit_baseline is not None else None
    income_shift_pct = round((income_avg - income_baseline) / income_baseline, 6) if income_avg is not None and income_baseline is not None and income_baseline > 0 else None
    dti_shift = round(dti_avg - dti_baseline, 6) if dti_avg is not None and dti_baseline is not None else None

    return {
        "credit_avg": credit_avg,
        "credit_baseline": credit_baseline,
        "credit_shift": credit_shift,
        "income_avg": income_avg,
        "income_baseline": income_baseline,
        "income_shift_pct": income_shift_pct,
        "dti_avg": dti_avg,
        "dti_baseline": dti_baseline,
        "dti_shift": dti_shift,
    }


def _fetch_decisions(limit: int = 200):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT
                d.id AS decision_id,
                d.application_id,
                d.created_at,
                d.engine_version,
                d.risk_score,
                d.decision,
                d.decision_contract_version,
                a.applicant_name,
                a.annual_income,
                a.loan_amount,
                a.credit_score,
                a.employment_years,
                a.source,
                e.reasons_json,
                e.reason_details_json
            FROM decisions d
            JOIN applications a ON a.id = d.application_id
            LEFT JOIN explanations e ON e.decision_id = d.id
            ORDER BY d.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        try:
            d["reasons"] = json.loads(d.pop("reasons_json", "[]") or "[]")
        except Exception:
            d["reasons"] = []
        try:
            d["reason_details"] = json.loads(d.pop("reason_details_json", "[]") or "[]")
        except Exception:
            d["reason_details"] = []
        result.append(d)
    return result


def _fetch_alerts(limit: int = 200):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = json.loads(d.pop("details_json", "{}") or "{}")
        except Exception:
            d["details"] = {}
        result.append(d)
    return result


@APP.get("/api/contract")
def api_contract():
    return jsonify({
        "contract_version": DECISION_CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
        "decision_types": ["APPROVE", "REVIEW", "REJECT"],
        "event_types": ALL_EVENT_TYPES,
        "inputs_schema": {
            "annual_income": "number",
            "loan_amount": "number",
            "credit_score": "int",
            "employment_years": "number",
            "applicant_name": "string?",
        },
        "reason_codes": ALL_REASON_CODES,
        "thresholds": {
            "review_threshold": REVIEW_THRESHOLD,
            "reject_threshold": REJECT_THRESHOLD,
        },
        "definitions": {
            "slip": "AUTO_APPROVE then HUMAN_REJECT, or AUTO_REJECT then HUMAN_APPROVE",
            "correction": "AUTO_REVIEW then HUMAN_APPROVE/HUMAN_REJECT",
        },
    })


@APP.get("/api/measure/summary")
def api_measure_summary():
    window = request.args.get("window", "7d")
    if window not in ("24h", "7d", "all"):
        window = "7d"
    with db() as conn:
        result = _compute_summary(conn, window)
    return jsonify(result)


@APP.get("/api/events")
def api_events():
    limit = min(int(request.args.get("limit", 500)), 2000)
    events = _fetch_events(limit)
    return jsonify(events)


@APP.get("/api/decisions")
def api_decisions():
    limit = min(int(request.args.get("limit", 200)), 2000)
    decisions = _fetch_decisions(limit)
    return jsonify(decisions)


@APP.get("/api/alerts")
def api_alerts():
    limit = min(int(request.args.get("limit", 200)), 2000)
    alerts = _fetch_alerts(limit)
    return jsonify(alerts)


init_db()
migrate_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    APP.run(host="0.0.0.0", port=port)
