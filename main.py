from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from flask import Flask, request, redirect, url_for, render_template_string, abort, jsonify

APP = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "loan_sim.db")
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
                sim_day TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                num_created INTEGER NOT NULL
            );
            """
        )


def migrate_db() -> None:
    with db() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(applications)").fetchall()}
        if "source" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "sim_day" not in cols:
            conn.execute("ALTER TABLE applications ADD COLUMN sim_day TEXT")

        conn.execute("""
        CREATE TABLE IF NOT EXISTS simulation_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sim_day TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          num_created INTEGER NOT NULL
        )
        """)


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
    dti = inp.loan_amount / max(inp.annual_income, 1.0)
    credit_risk = clamp((720 - inp.credit_score) / 320.0, 0.0, 1.0)
    dti_risk = clamp((dti - 0.15) / 0.85, 0.0, 1.0)
    emp_risk = clamp((2.0 - inp.employment_years) / 2.0, 0.0, 1.0)
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

    details.sort(key=lambda d: d["impact"], reverse=True)
    return risk_score, details[:3]


def decide(risk_score: float, inp: AppInput) -> Tuple[str, bool]:
    approve_th = 0.45
    review_th = 0.55

    near_band = 0.03
    near_threshold = (abs(risk_score - approve_th) <= near_band) or (abs(risk_score - review_th) <= near_band)

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
  </div>

  <h1>Decision</h1>

  <p>
    Applicant: <b>{{applicant_name or "\u2014"}}</b>
    <span class="pill">{{decision}}</span>
    <span class="pill">risk={{risk_score}}</span>
    <span class="pill">{{engine_version}}</span>
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
  <ul>
  {% for r in reasons %}
    <li><b>{{r.code}}</b>: {{r.text}} (value={{r.value}}, impact={{r.impact}})</li>
  {% endfor %}
  </ul>

  <h3>Raw explanation JSON</h3>
  <pre>{{reason_details_json}}</pre>
</body>
</html>
"""


# -----------------------------
# Routes
# -----------------------------


def create_application_and_decide(inp: AppInput, source: str = "manual", sim_day: str | None = None):
    risk_score, reason_details = compute_risk_and_reasons(inp)
    decision, needs_review = decide(risk_score, inp)

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

        cur = conn.execute(
            """
            INSERT INTO decisions (application_id, created_at, engine_version, risk_score, decision)
            VALUES (?, ?, ?, ?, ?)
            """,
            (app_id, created_at, ENGINE_VERSION, float(round(risk_score, 4)), decision),
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

        if needs_review:
            conn.execute(
                "INSERT INTO review_tasks (decision_id, created_at, status) VALUES (?, ?, 'PENDING')",
                (decision_id, created_at),
            )

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
    sim_day = datetime.now(timezone.utc).date().isoformat()

    with db() as conn:
        if conn.execute("SELECT 1 FROM simulation_runs WHERE sim_day = ?", (sim_day,)).fetchone():
            return redirect(url_for("recent"))

    rng = random.Random(sim_day)

    samples = [
        AppInput(f"sim-{sim_day}-A", rng.randint(80000, 140000),
                 rng.randint(5000, 20000), rng.randint(720, 810),
                 round(rng.uniform(3, 10), 1)),
        AppInput(f"sim-{sim_day}-B", rng.randint(40000, 80000),
                 rng.randint(15000, 40000), rng.randint(560, 680),
                 round(rng.uniform(0.5, 3.5), 1)),
        AppInput(f"sim-{sim_day}-C", rng.randint(25000, 60000),
                 rng.randint(30000, 90000), rng.randint(420, 590),
                 round(rng.uniform(0, 2.0), 1)),
    ]

    for s in samples:
        create_application_and_decide(s, source="simulated", sim_day=sim_day)

    with db() as conn:
        conn.execute(
            "INSERT INTO simulation_runs (sim_day, created_at, num_created) VALUES (?, ?, 3)",
            (sim_day, utc_now_iso()),
        )

    return redirect(url_for("recent"))


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
        annual_income=row["annual_income"],
        loan_amount=row["loan_amount"],
        credit_score=row["credit_score"],
        employment_years=row["employment_years"],
        app_source=row["source"],
        sim_day=row["sim_day"],
        reasons=reasons,
        reason_details_json=raw_json,
    )


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


if __name__ == "__main__":
    init_db()
    migrate_db()
    port = int(os.environ.get("PORT", 5000))
    APP.run(host="0.0.0.0", port=port)
