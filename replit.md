# Loan Decision Contract

## Overview
A basic Python application that evaluates loan applications based on various criteria including income, credit score, loan amount, and employment history.

## Project Structure
- `main.py` - Main application file containing the loan evaluation logic

## How It Works
The application uses a scoring system to evaluate loan applications:
- **Income Ratio**: Compares loan amount to annual income
- **Credit Score**: Evaluates creditworthiness (300-850 scale)
- **Employment History**: Considers years of stable employment

### Decision Outcomes
- **APPROVED**: Score >= 70
- **CONDITIONAL APPROVAL**: Score 50-69
- **DENIED**: Score < 50

## Running the Application
```bash
python main.py
```

## Recent Changes
- February 2026: Initial project setup
