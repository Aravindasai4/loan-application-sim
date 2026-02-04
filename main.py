"""
Loan Decision Contract
A basic Python application for evaluating loan applications.
"""


def evaluate_loan(income: float, credit_score: int, loan_amount: float, employment_years: int) -> dict:
    """
    Evaluate a loan application based on provided criteria.
    
    Args:
        income: Annual income of the applicant
        credit_score: Credit score (300-850)
        loan_amount: Requested loan amount
        employment_years: Years of employment
    
    Returns:
        Dictionary containing decision and details
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


def main():
    print("=" * 50)
    print("       LOAN DECISION CONTRACT SYSTEM")
    print("=" * 50)
    print()
    
    # Example loan applications
    applications = [
        {"income": 75000, "credit_score": 720, "loan_amount": 150000, "employment_years": 6},
        {"income": 45000, "credit_score": 580, "loan_amount": 200000, "employment_years": 1},
        {"income": 100000, "credit_score": 800, "loan_amount": 250000, "employment_years": 10},
    ]
    
    for i, app in enumerate(applications, 1):
        print(f"Application #{i}")
        print(f"  Income: ${app['income']:,}")
        print(f"  Credit Score: {app['credit_score']}")
        print(f"  Loan Amount: ${app['loan_amount']:,}")
        print(f"  Employment: {app['employment_years']} years")
        
        result = evaluate_loan(**app)
        
        print(f"\n  Decision: {result['decision']}")
        print(f"  Score: {result['score']}/100")
        print(f"  Income Ratio: {result['income_ratio']}x")
        print("  Factors:")
        for reason in result['reasons']:
            print(f"    - {reason}")
        print("-" * 50)
        print()


if __name__ == "__main__":
    main()
