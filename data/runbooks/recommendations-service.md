# Runbook: recommendations-service

**ID:** RB-recommendations-service · **Owner:** Ishita Sen (Personalization) · **Namespace:** recommendations · **Version:** 1.0

## Safe triage
1. Open the recommendations-service service dashboard and set the window to 30 minutes before the alert.
2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.
3. Inspect recent changes: `kubectl -n recommendations rollout history deploy/recommendations-service`.
4. Compare dependency health and check whether impact is isolated to one region.

## Change safety
Every mutation requires an incident note, verification metric and rollback command. Destructive database, queue and cache commands require service-owner approval.

## Escalation
Escalate to Ishita Sen. If multiple services or infrastructure are affected, include Platform/SRE.
