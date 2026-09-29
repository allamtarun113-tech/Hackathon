# Runbook: orders-service

**Owner:** Arjun Mehta (Orders) · **Namespace:** orders

## Symptoms
Orders stuck in PENDING, consumer lag alerts.

## Steps
1. Check consumer lag in the orders-service dashboard.
2. Scale consumers: `kubectl -n orders scale deploy/orders-consumer --replicas=12`.
3. Restart consumers: `kubectl -n orders rollout restart deploy/orders-consumer`.
4. Check the last deploy of orders-service and roll back if needed.
5. Escalate to Orders team.
