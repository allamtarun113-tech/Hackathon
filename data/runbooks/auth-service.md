# Runbook: auth-service

**Owner:** Neha Iyer (Identity) · **Namespace:** identity

## Symptoms
Slow or failing logins, OTP errors.

## Steps
1. Check the auth-service dashboard (login latency, error rate).
2. Restart pods: `kubectl -n identity rollout restart deploy/auth-service`.
3. Scale out during sales: `kubectl -n identity scale deploy/auth-service --replicas=8`.
4. Check Redis is reachable: `redis-cli -h redis-cache PING`.
5. Escalate to Identity team.
