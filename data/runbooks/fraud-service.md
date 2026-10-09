# Runbook: fraud-service

**ID:** RB-fraud-service · **Owner:** Zoya Ali (Risk) · **Namespace:** fraud · **Version:** 1.0

## Safe triage
1. Open the fraud-service service dashboard and set the window to 30 minutes before the alert.
2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.
3. Inspect recent changes: `kubectl -n fraud rollout history deploy/fraud-service`.
4. Compare dependency health and check whether impact is isolated to one region.

## Change safety
Every mutation requires an incident note, verification metric and rollback command. Destructive database, queue and cache commands require service-owner approval.

## Escalation
Escalate to Zoya Ali. If multiple services or infrastructure are affected, include Platform/SRE.
