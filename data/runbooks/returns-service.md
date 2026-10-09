# Runbook: returns-service

**ID:** RB-returns-service · **Owner:** Nikhil Rao (Post-purchase) · **Namespace:** returns · **Version:** 1.0

## Safe triage
1. Open the returns-service service dashboard and set the window to 30 minutes before the alert.
2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.
3. Inspect recent changes: `kubectl -n returns rollout history deploy/returns-service`.
4. Compare dependency health and check whether impact is isolated to one region.

## Change safety
Every mutation requires an incident note, verification metric and rollback command. Destructive database, queue and cache commands require service-owner approval.

## Escalation
Escalate to Nikhil Rao. If multiple services or infrastructure are affected, include Platform/SRE.
