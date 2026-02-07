from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from flask import Flask, request, redirect, url_for, render_template_string, abort

APP = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "loan_sim.db")
ENGINE_VERSION = "rules-v0.1"


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
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                risk_score REAL NOT NULL,
                decision TEXT NOT NULL, -- APPROVE/REVIEW/REJECT
                FOREIGN KEY(application_id) REFERENCES applications(id)
            );

            CREATE TABLE IF NOT EXISTS explanations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reasons_json TEXT NOT NULL, -- list[str]
                reason_details_json TEXT NOT NULL, -- list[{"code":..,"text":..,"value":..}]
                FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );

            CREATE TABLE IF NOT EXISTS review_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL, -- PENDING/RESOLVED
                resolved_at TEXT,
                human_outcome TEXT, -- APPROVE/REJECT
                human_notes TEXT,
                FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );
            """
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def compute_risk_and_reasons(inp: AppInput) -> Tuple[float, List[Dict]]:
    """
    Returns:
      risk_score in [0,1] where higher = riskier (more likely to default)
      reason_details: list of dicts with code/text/value/impact
    """
    # Derived features
    dti = inp.loan_amount / max(inp.annual_income, 1.0)  # debt-to-income proxy (rough)

    # Normalize signals into 0..1 risk contributions
    # Credit score: lower score => higher risk
    credit_risk = clamp((720 - inp.credit_score) / 320.0, 0.0, 1.0)  # ~400-720 range

    # DTI: higher => higher risk
    dti_risk = clamp((dti - 0.15) / 0.85, 0.0, 1.0)  # 0.15 safe-ish, >1.0 very risky

    # Employment: shorter => higher risk
    emp_risk = clamp((2.0 - inp.employment_years) / 2.0, 0.0, 1.0)  # <2y risky

    # Weighted risk score
    risk_score = clamp(0.55 * credit_risk + 0.30 * dti_risk + 0.15 * emp_risk, 0.0, 1.0)

    details = [
        {
            "code": "CREDIT_SCORE_LOW",
            "text": "Credit score is below the preferred range.",
            "value": inp.credit_score,
            "impact": round(0.55 * credit_risk, 4),
        },
        {
            "code": "DTI_HIGH",
            "text": "Requested loan amount is high relative to annual income (DTI proxy).",
            "value": round(dti, 4),
            "impact": round(0.30 * dti_risk, 4),
        },
        {
            "code": "EMPLOYMENT_SHORT",
            "text": "Employment history is short, which increases repayment uncertainty.",
            "value": inp.employment_years,
            "impact": round(0.15 * emp_risk, 4),
        },
    ]

    # Sort by impact and keep top 3
    details.sort(key=lambda d: d["impact"], reverse=True)
    return risk_score, details[:3]


def decide(risk_score: float, inp: AppInput) -> Tuple[str, bool]:
    """
    Returns: (decision, needs_review)
    """
    # Policy thresholds (tweak later)
    approve_th = 0.45
    review_th = 0.55

    # Edge-case gating: near-threshold => review
    near_band = 0.03
    near_threshold = (abs(risk_score - approve_th) <= near_band) or (abs(risk_score - review_th) <= near_band)

    # Additional hard flags to force review
    forced_review = False
    if inp.credit_score < 520:
        forced_review = True
    if inp.annual_income <= 0 or inp.loan_amount <= 0:
        forced_review = True

    if forced_review or near_threshold:
        return "REVIEW", True
    if risk_score < approve_th:
        return "APPROVE", False
    if risk_score <= review_th:
        return "REVIEW", True
    return "REJECT", False


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
  </div>

  <h1>Loan Application (Simulator)</h1>
  <p class="hint">This is a simulator. It logs decisions + explanations and routes edge cases to human review.</p>

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
  </div>

  <h1>Decision</h1>
  <p>
    Outcome:
    <span class="pill {{decision}}">{{decision}}</span>
    &nbsp;| Risk score: <code>{{risk_score}}</code>
    &nbsp;| Engine: <code>{{engine_version}}</code>
  </p>

  <h2>Top Reasons</h2>
  <ul>
    {% for r in reasons %}
      <li><b>{{r["code"]}}</b>: {{r["text"]}} (value={{r["value"]}}, impact={{r["impact"]}})</li>
    {% endfor %}
  </ul>

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
          <td>{{r["applicant_name"] or ""}}</td>
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


# -----------------------------
# Routes
# -----------------------------
@APP.get("/")
def root():
    return redirect(url_for("apply_get"))


@APP.get("/apply")
def apply_get():
    return render_template_string(APPLY_HTML)


@APP.post("/apply")
def apply_post():
    # Parse and validate inputs
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

    risk_score, reason_details = compute_risk_and_reasons(inp)
    decision, needs_review = decide(risk_score, inp)

    # Write to DB
    created_at = utc_now_iso()
    raw = {
        "applicant_name": inp.applicant_name,
        "annual_income": inp.annual_income,
        "loan_amount": inp.loan_amount,
        "credit_score": inp.credit_score,
        "employment_years": inp.employment_years,
    }

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO applications
              (created_at, applicant_name, annual_income, loan_amount, credit_score, employment_years, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (created_at, inp.applicant_name, inp.annual_income, inp.loan_amount, inp.credit_score, inp.employment_years, json.dumps(raw)),
        )
        app_id = cur.lastrowid

        cur = conn.execute(
            """
            INSERT INTO decisions (application_id, created_at, engine_version, risk_score, decision)
            VALUES (?, ?, ?, ?, ?)
            """,
            (app_id, created_at, ENGINE_VERSION, float(round(risk_score, 4)), decision),
        )
        decision_id = cur.lastrowid

        reasons_json = json.dumps([d["code"] for d in reason_details])
        reason_details_json = json.dumps(reason_details)
        conn.execute(
            """
            INSERT INTO explanations (decision_id, created_at, reasons_json, reason_details_json)
            VALUES (?, ?, ?, ?)
            """,
            (decision_id, created_at, reasons_json, reason_details_json),
        )

        if needs_review:
            conn.execute(
                """
                INSERT INTO review_tasks (decision_id, created_at, status)
                VALUES (?, ?, 'PENDING')
                """,
                (decision_id, created_at),
            )

    return render_template_string(
        RESULT_HTML,
        decision=decision,
        risk_score=round(risk_score, 4),
        reasons=reason_details,
        engine_version=ENGINE_VERSION,
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
            JOIN explanations e ON e.decision_id = d.id
            WHERE rt.status = 'PENDING'
            ORDER BY rt.created_at DESC
            """
        ).fetchall()

    tasks = []
    for r in rows:
        reasons = json.loads(r["reason_details_json"])
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
        row = conn.execute("SELECT id, status FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            abort(404, "Task not found.")
        if row["status"] != "PENDING":
            abort(400, "Task already resolved.")

        conn.execute(
            """
            UPDATE review_tasks
            SET status='RESOLVED', resolved_at=?, human_outcome=?, human_notes=?
            WHERE id=?
            """,
            (utc_now_iso(), outcome, notes, task_id),
        )

    return redirect(url_for("review_queue"))


@APP.get("/recent")
def recent():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT d.created_at, a.applicant_name, d.decision, d.risk_score, d.engine_version
            FROM decisions d
            JOIN applications a ON a.id = d.application_id
            ORDER BY d.created_at DESC
            LIMIT 50
            """
        ).fetchall()
    return render_template_string(RECENT_HTML, rows=rows)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    APP.run(host="0.0.0.0", port=port)
