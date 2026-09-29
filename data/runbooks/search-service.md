# Runbook: search-service

**Owner:** Vikram Rao (Search) · **Namespace:** search

## Symptoms
Search returning errors or empty results, pods restarting.

## Steps
1. Check pod status: `kubectl -n search get pods`.
2. Restart pods: `kubectl -n search rollout restart deploy/search-service`.
3. Check the dashboard for query latency.
4. Escalate to Search team.
