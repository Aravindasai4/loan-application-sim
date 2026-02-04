# Loan Decision Contract

## Overview
A Flask web application that evaluates loan applications based on various criteria including income, credit score, loan amount, and employment history.

## Project Structure
- `main.py` - Flask web server with loan evaluation logic and UI

## How It Works
The application uses a scoring system to evaluate loan applications:
- **Income Ratio**: Compares loan amount to annual income
- **Credit Score**: Evaluates creditworthiness (300-850 scale)
- **Employment History**: Considers years of stable employment

### Decision Outcomes
- **APPROVED**: Score >= 70
- **CONDITIONAL APPROVAL**: Score 50-69
- **DENIED**: Score < 50

### API Endpoints
- `GET /` - Web interface for loan evaluation
- `POST /evaluate` - JSON API for loan evaluation

## Running the Application
```bash
python main.py
```
The server runs on port 5000.

## Deployment
Configured for Replit autoscale deployment with `python main.py` as the run command.

## Recent Changes
- February 2026: Converted to Flask web application for deployment support
