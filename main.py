"""
Loan Decision Contract
A web application for evaluating loan applications.
"""

import os
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)


def evaluate_loan(income: float, credit_score: int, loan_amount: float, employment_years: int) -> dict:
    """
    Evaluate a loan application based on provided criteria.
    """
    reasons = []
    score = 0
    
    # Income to loan ratio check
    income_ratio = loan_amount / income if income > 0 else float('inf')
    if income_ratio <= 3:
        score += 30
        reasons.append("Good income to loan ratio")
    elif income_ratio <= 5:
        score += 15
        reasons.append("Acceptable income to loan ratio")
    else:
        reasons.append("High loan amount relative to income")
    
    # Credit score evaluation
    if credit_score >= 750:
        score += 40
        reasons.append("Excellent credit score")
    elif credit_score >= 650:
        score += 25
        reasons.append("Good credit score")
    elif credit_score >= 550:
        score += 10
        reasons.append("Fair credit score")
    else:
        reasons.append("Poor credit score")
    
    # Employment history
    if employment_years >= 5:
        score += 30
        reasons.append("Strong employment history")
    elif employment_years >= 2:
        score += 20
        reasons.append("Adequate employment history")
    elif employment_years >= 1:
        score += 10
        reasons.append("Limited employment history")
    else:
        reasons.append("Insufficient employment history")
    
    # Make decision
    if score >= 70:
        decision = "APPROVED"
    elif score >= 50:
        decision = "CONDITIONAL APPROVAL"
    else:
        decision = "DENIED"
    
    return {
        "decision": decision,
        "score": score,
        "reasons": reasons,
        "income_ratio": round(income_ratio, 2)
    }


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Loan Decision Contract</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; min-height: 100vh; padding: 40px 20px; }
        .container { max-width: 600px; margin: 0 auto; }
        h1 { text-align: center; color: #333; margin-bottom: 30px; }
        .card { background: white; border-radius: 12px; padding: 30px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 8px; font-weight: 500; color: #555; }
        input { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 16px; }
        input:focus { outline: none; border-color: #007bff; }
        button { width: 100%; padding: 14px; background: #007bff; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; margin-top: 10px; }
        button:hover { background: #0056b3; }
        .result { margin-top: 30px; padding: 20px; border-radius: 8px; display: none; }
        .result.approved { background: #d4edda; border: 1px solid #c3e6cb; }
        .result.conditional { background: #fff3cd; border: 1px solid #ffeeba; }
        .result.denied { background: #f8d7da; border: 1px solid #f5c6cb; }
        .result h2 { margin-bottom: 15px; }
        .result p { margin: 8px 0; color: #555; }
        .reasons { margin-top: 15px; }
        .reasons li { margin: 5px 0; color: #666; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Loan Decision Contract</h1>
        <div class="card">
            <form id="loanForm">
                <div class="form-group">
                    <label for="income">Annual Income ($)</label>
                    <input type="number" id="income" name="income" required min="0" placeholder="e.g., 75000">
                </div>
                <div class="form-group">
                    <label for="credit_score">Credit Score (300-850)</label>
                    <input type="number" id="credit_score" name="credit_score" required min="300" max="850" placeholder="e.g., 720">
                </div>
                <div class="form-group">
                    <label for="loan_amount">Loan Amount ($)</label>
                    <input type="number" id="loan_amount" name="loan_amount" required min="0" placeholder="e.g., 150000">
                </div>
                <div class="form-group">
                    <label for="employment_years">Years of Employment</label>
                    <input type="number" id="employment_years" name="employment_years" required min="0" placeholder="e.g., 5">
                </div>
                <button type="submit">Evaluate Loan Application</button>
            </form>
            <div id="result" class="result">
                <h2 id="decision"></h2>
                <p><strong>Score:</strong> <span id="score"></span>/100</p>
                <p><strong>Income Ratio:</strong> <span id="ratio"></span>x</p>
                <div class="reasons">
                    <strong>Factors:</strong>
                    <ul id="reasons"></ul>
                </div>
            </div>
        </div>
    </div>
    <script>
        document.getElementById('loanForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = Object.fromEntries(formData);
            
            const response = await fetch('/evaluate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    income: parseFloat(data.income),
                    credit_score: parseInt(data.credit_score),
                    loan_amount: parseFloat(data.loan_amount),
                    employment_years: parseInt(data.employment_years)
                })
            });
            
            const result = await response.json();
            const resultDiv = document.getElementById('result');
            
            resultDiv.className = 'result';
            if (result.decision === 'APPROVED') resultDiv.classList.add('approved');
            else if (result.decision === 'CONDITIONAL APPROVAL') resultDiv.classList.add('conditional');
            else resultDiv.classList.add('denied');
            
            document.getElementById('decision').textContent = result.decision;
            document.getElementById('score').textContent = result.score;
            document.getElementById('ratio').textContent = result.income_ratio;
            
            const reasonsList = document.getElementById('reasons');
            reasonsList.innerHTML = result.reasons.map(r => `<li>${r}</li>`).join('');
            
            resultDiv.style.display = 'block';
        });
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route('/evaluate', methods=['POST'])
def evaluate():
    data = request.get_json()
    result = evaluate_loan(
        income=data['income'],
        credit_score=data['credit_score'],
        loan_amount=data['loan_amount'],
        employment_years=data['employment_years']
    )
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
