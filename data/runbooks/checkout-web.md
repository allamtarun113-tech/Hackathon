# Runbook: checkout-web

**Owner:** Arjun Mehta (Checkout) · **Namespace:** checkout

## Symptoms
5xx on /api/checkout, users cannot place orders.

## Steps
1. Check the checkout-web dashboard (5xx rate, p95 latency).
2. Scale out: `kubectl -n checkout scale deploy/checkout-web --replicas=12`.
3. Roll back the last deploy: `kubectl -n checkout rollout undo deploy/checkout-web`.
4. Check downstream services (payments-api, orders-service) dashboards.
5. Escalate to Checkout team.
