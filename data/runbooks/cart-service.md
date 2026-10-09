# Runbook: cart-service

**ID:** RB-cart-service · **Owner:** Dev Patel (Cart) · **Namespace:** cart · **Version:** 1.0

## Safe triage
1. Open the cart-service service dashboard and set the window to 30 minutes before the alert.
2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.
3. Inspect recent changes: `kubectl -n cart rollout history deploy/cart-service`.
4. Compare dependency health and check whether impact is isolated to one region.

## Change safety
Every mutation requires an incident note, verification metric and rollback command. Destructive database, queue and cache commands require service-owner approval.

## Escalation
Escalate to Dev Patel. If multiple services or infrastructure are affected, include Platform/SRE.
