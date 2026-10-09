# Runbook: inventory-service

**ID:** RB-inventory-service · **Owner:** Ananya Gupta (Orders & Checkout) · **Namespace:** inventory · **Version:** 1.0

## Safe triage
1. Open the inventory-service service dashboard and set the window to 30 minutes before the alert.
2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.
3. Inspect recent changes: `kubectl -n inventory rollout history deploy/inventory-service`.
4. Compare dependency health and check whether impact is isolated to one region.

## Change safety
Every mutation requires an incident note, verification metric and rollback command. Destructive database, queue and cache commands require service-owner approval.

## Escalation
Escalate to Ananya Gupta. If multiple services or infrastructure are affected, include Platform/SRE.
