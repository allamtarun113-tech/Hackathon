# Runbook: notification-service

**Owner:** Neha Iyer (Notifications) · **Namespace:** notifications

## Symptoms
SMS/email/push not delivered, delivery failure rate high.

## Steps
1. Check the notification-service dashboard (delivery success rate by channel).
2. Restart pods: `kubectl -n notifications rollout restart deploy/notification-service`.
3. Check the SMS provider status page.
4. Escalate to Notifications team.
