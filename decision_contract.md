# Decision Contract — Loan Approval AI

**Version:** v0.2  
**Last Updated:** March 2026  

## 1. System Name
Automated Loan Decision Engine (rules-v0.2)

## 2. Decision / Output
This system approves, flags for review, or rejects loan applications and assigns 
a numerical risk score (0.0–1.0) with reason codes explaining the decision.

## 3. Trigger
The decision is produced when a loan application is submitted containing: 
annual income, loan amount, credit score, and employment years.

## 4. Affected Parties
**Primary:** Loan applicants (financial access, credit history impact)  
**Secondary:** Credit risk team, human reviewers, compliance officers  
**Indirect:** Applicant dependents, regulators, auditors  

## 5. Decision Authority
☑ Gated — AI output is binding for APPROVE/REJECT.  
REVIEW outcomes require mandatory human resolution before finalization.

## 6. Thresholds
- Risk score < 0.40 → APPROVE  
- Risk score 0.40–0.70 → REVIEW (human required)  
- Risk score > 0.70 → REJECT  

## 7. What Counts as Harm
**Individual:** Unjust denial of credit; approval leading to unmanageable debt; 
inability to explain a rejection  
**Group/Societal:** Disparate impact on vulnerable groups; reinforcement of 
historical lending discrimination  
**Organizational/Legal:** Regulatory non-compliance (ECOA, Fair Lending); 
inability to defend decisions during audit  

## 8. Unacceptable Outcomes
- Denying credit without an explainable reason code
- Review rate dropping below 10% (insufficient human oversight)
- Input feature drift exceeding baseline thresholds without an alert
- Engine version mismatch with contract version in production
- Audit trail gaps (decisions without linked events)

## 9. Assumptions & Constraints
- Applicant data is self-reported and unverified
- Engine version must match contract version (currently rules-v0.2 / v0.2)
- Simulation traffic is tagged separately from manual/production traffic
- Model does not use protected attributes (race, gender, religion)

## 10. Accountability & Traceability
Every decision is logged with: engine version, risk score, reason codes, 
contract version, and UTC timestamp. Human overrides are recorded in the 
audit trail with outcome and notes. All events are immutable once written.

## 11. Metrics This Contract Enables
- Review rate (target: 15–30%)
- Slip rate (false negatives caught by human review)
- Correction rate (false positives caught by human review)  
- Input drift delta (credit score, income, DTI vs baseline)
- Audit trail completeness (decisions with linked explanations)