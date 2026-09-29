# Runbook: payments-api

**Owner:** Priya Sharma (Payments) · **Namespace:** payments

## Symptoms
High latency or 5xx on /v1/charges or /v1/refunds.

## Steps
1. Check the payments-api Grafana dashboard (latency, error rate, JVM heap).
2. Check the last deploy: `kubectl -n payments rollout history deploy/payments-api`.
3. Restart pods: `kubectl -n payments rollout restart deploy/payments-api`.
4. Scale out if CPU > 70%: `kubectl -n payments scale deploy/payments-api --replicas=10`.
5. Roll back the last deploy if errors started after it.
6. Escalate to the Payments team.
