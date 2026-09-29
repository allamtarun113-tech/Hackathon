# Incident Response Basics

1. Acknowledge the page within 5 minutes.
2. Open the service dashboard in Grafana and note error rate, latency and saturation.
3. Check what changed recently:
   `kubectl -n <namespace> rollout history deploy/<service>`
4. If a deploy went out in the last hour, consider rolling it back:
   `kubectl -n <namespace> rollout undo deploy/<service>`
5. If pods look unhealthy, restart them:
   `kubectl -n <namespace> rollout restart deploy/<service>`
6. If the service is saturated, scale it out:
   `kubectl -n <namespace> scale deploy/<service> --replicas=<n>`
7. If there is no progress after 20 minutes, escalate to the service owner.
8. Post updates in #incidents every 15 minutes.
9. After resolution, write a postmortem within 48 hours.
