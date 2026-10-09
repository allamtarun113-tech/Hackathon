"""Deterministically expand PagerMind's synthetic corpus to production-demo scale.

The script preserves the original 61 hand-authored incidents and 8 curated demo
scenarios, then creates deterministic, labelled additions:

* 500 total historical incidents across 15 services
* 100 total held-out evaluation scenarios
* 800 total deploy/change records
* one runbook per service

Generated records carry ``data_provenance.generator`` so the script is safe to
re-run: previous generated rows are replaced while the original rows remain.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
GENERATOR = "pagermind-production-corpus-v1"
SEED = 20261001
IST = timezone(timedelta(hours=5, minutes=30))

TARGET_INCIDENTS = 500
TARGET_SCENARIOS = 100
TARGET_DEPLOYS = 800

SERVICE_INFO = {
    "checkout-web": ("checkout", "Arjun Mehta", "Orders & Checkout"),
    "payments-api": ("payments", "Priya Sharma", "Payments"),
    "orders-service": ("orders", "Arjun Mehta", "Orders & Checkout"),
    "inventory-service": ("inventory", "Ananya Gupta", "Orders & Checkout"),
    "auth-service": ("identity", "Neha Iyer", "Identity & Notifications"),
    "search-service": ("search", "Vikram Rao", "Search & Discovery"),
    "notification-service": ("notifications", "Neha Iyer", "Identity & Notifications"),
    "pricing-service": ("pricing", "Meera Kulkarni", "Pricing & Promotions"),
    "cart-service": ("cart", "Dev Patel", "Cart"),
    "recommendations-service": ("recommendations", "Ishita Sen", "Personalization"),
    "shipping-service": ("shipping", "Rahul Verma", "Fulfilment"),
    "fraud-service": ("fraud", "Zoya Ali", "Risk"),
    "returns-service": ("returns", "Nikhil Rao", "Post-purchase"),
    "catalog-service": ("catalog", "Sana Mirza", "Catalog"),
    "api-gateway": ("edge", "Aditya Bose", "Edge Platform"),
}

NEW_ENGINEERS = [
    ("meera", "Meera Kulkarni", "Staff Engineer", "Pricing & Promotions"),
    ("dev", "Dev Patel", "Senior Software Engineer", "Cart"),
    ("ishita", "Ishita Sen", "Staff ML Engineer", "Personalization"),
    ("rahul", "Rahul Verma", "Senior Software Engineer", "Fulfilment"),
    ("zoya", "Zoya Ali", "Staff Engineer", "Risk"),
    ("nikhil", "Nikhil Rao", "Senior Software Engineer", "Post-purchase"),
    ("sana", "Sana Mirza", "Staff Engineer", "Catalog"),
    ("aditya", "Aditya Bose", "Senior SRE", "Edge Platform"),
]


def pat(
    pid: str,
    name: str,
    services: list[str],
    signature: str,
    root: str,
    failed: str,
    fix: str,
    command: str,
) -> dict[str, Any]:
    return {
        "id": pid,
        "name": name,
        "services": services,
        "signature": signature,
        "root": root,
        "failed": failed,
        "fix": fix,
        "command": command,
    }


PATTERNS = [
    pat("P1", "database pool exhaustion", ["payments-api", "inventory-service", "pricing-service", "returns-service"],
        "connection pool acquisition timed out; active equals maximum and waiting is rising",
        "A scheduled bulk job held database connections during peak traffic and exhausted the application pool.",
        "Restarted application pods", "Paused the bulk job and restored safe pool headroom",
        "kubectl -n {ns} suspend cronjob/bulk-maintenance"),
    pat("P2", "Redis keys without TTL", ["auth-service", "cart-service", "pricing-service", "fraud-service"],
        "OOM command not allowed; maxmemory reached and too few keys have expiry",
        "A new key family was written without TTL until Redis reached maxmemory and rejected writes.",
        "Scaled the application deployment", "Deleted the stale key family and enforced a TTL",
        "redis-cli --scan --pattern 'stale:*' | head"),
    pat("P3", "event schema incompatibility", ["orders-service", "inventory-service", "catalog-service", "shipping-service", "returns-service"],
        "event deserialization failed: required field missing; consumer partition paused",
        "A producer shipped a backward-incompatible event schema while registry compatibility was disabled.",
        "Restarted the consumer", "Restored the compatible schema and replayed the dead-letter topic",
        "kubectl -n {ns} set env deploy/{service} EVENT_SCHEMA_MODE=compatible"),
    pat("P4", "external provider timeout", ["payments-api", "shipping-service", "notification-service"],
        "upstream provider read timeout after 10000ms; circuit breaker open",
        "The primary external provider degraded while the application and database remained healthy.",
        "Rolled back the application", "Failed over traffic to the healthy secondary provider",
        "kubectl -n {ns} set env deploy/{service} PROVIDER_PRIMARY=secondary"),
    pat("P5", "batch reindex OOM", ["search-service", "recommendations-service", "catalog-service"],
        "container OOMKilled during full index rebuild; working set exceeded limit",
        "A full rebuild loaded the complete dataset into memory instead of processing bounded batches.",
        "Increased replica count", "Restored bounded batch processing and restarted one worker at a time",
        "kubectl -n {ns} set env deploy/{service} REINDEX_BATCH_SIZE=25000"),
    pat("P6", "internal certificate expiry", ["notification-service", "api-gateway", "fraud-service", "shipping-service"],
        "x509 certificate has expired or is not yet valid during internal TLS handshake",
        "An internal certificate expired because its issuer reference no longer matched the active issuer.",
        "Restarted the client pods", "Renewed the certificate and corrected the issuer reference",
        "kubectl -n {ns} annotate certificate internal-tls force-renewal=true"),
    pat("P7", "unsafe feature flag rollout", ["checkout-web", "cart-service", "pricing-service", "recommendations-service"],
        "request panic after feature variant enabled for one hundred percent of traffic",
        "A feature flag reached full traffic without coverage for a valid edge-case payload.",
        "Scaled out the deployment", "Disabled the feature flag and drained affected requests",
        "kubectl -n {ns} set env deploy/{service} FEATURE_VARIANT=off"),
    pat("P8", "read replica lag", ["orders-service", "catalog-service", "returns-service", "pricing-service"],
        "read replica lag above 180 seconds; record missing on replica but present on primary",
        "A write-heavy maintenance task flooded WAL and delayed replay on the read replica.",
        "Restarted the API pods", "Moved reads to the primary and throttled the write-heavy task",
        "kubectl -n {ns} set env deploy/{service} DB_READ_FROM_REPLICA=false"),
    pat("P9", "cluster capacity ceiling", ["checkout-web", "api-gateway", "recommendations-service", "cart-service"],
        "pod scheduling failed: insufficient cpu; cluster autoscaler maximum size reached",
        "Traffic exceeded the node-group ceiling, leaving required application replicas Pending.",
        "Manually increased application replicas", "Raised the node-group maximum and pre-scaled critical workloads",
        "clusterctl nodegroup update ng-general --max-nodes 120"),
    pat("P10", "goroutine or worker leak", ["notification-service", "recommendations-service", "fraud-service", "shipping-service"],
        "worker count continuously increasing; memory above ninety percent; OOM kill imminent",
        "A retry path leaked workers after reconnects, causing slow memory growth between deployments.",
        "Only increased the memory limit", "Performed a rolling restart and disabled the leaking retry path",
        "kubectl -n {ns} rollout restart deploy/{service}"),
    pat("P11", "edge security false positive", ["auth-service", "api-gateway", "checkout-web"],
        "edge security rule blocked valid request body; origin request volume dropped",
        "A managed edge rule incorrectly classified a valid client payload as an attack.",
        "Restarted the origin service", "Scoped the edge-rule exception to the affected route and payload field",
        "edge-waf ruleset patch --route /v1/session --mode log"),
    pat("P12", "promotion cache staleness", ["pricing-service", "checkout-web", "cart-service"],
        "price version mismatch between cache and source; promotion epoch is stale",
        "Promotion invalidation messages were dropped, leaving stale prices in the application cache.",
        "Restarted one pod", "Invalidated the affected price epoch and replayed invalidation events",
        "kubectl -n {ns} exec deploy/{service} -- cachectl invalidate price-epoch"),
    pat("P13", "cart hot-key saturation", ["cart-service", "checkout-web", "auth-service"],
        "redis hot key exceeded operation budget; command latency above 2000ms",
        "A shared campaign key concentrated most cart traffic on one Redis shard.",
        "Raised API replica count", "Sharded the campaign key and rate-limited expensive recomputation",
        "kubectl -n {ns} set env deploy/{service} CAMPAIGN_KEY_SHARDS=64"),
    pat("P14", "feature store timeout", ["recommendations-service", "fraud-service", "pricing-service"],
        "online feature lookup deadline exceeded; feature vector incomplete",
        "Feature-store compaction saturated I/O and caused online lookups to miss their deadline.",
        "Restarted model-serving pods", "Paused compaction and enabled the last-known-good feature fallback",
        "featurectl compaction pause --cluster online"),
    pat("P15", "carrier API throttling", ["shipping-service", "checkout-web", "returns-service"],
        "carrier quote rejected with 429; retry-after budget exhausted",
        "A carrier reduced the account quota while unjittered retries amplified request volume.",
        "Retried all requests immediately", "Failed over carriers and enabled exponential retry jitter",
        "kubectl -n {ns} set env deploy/{service} CARRIER_FAILOVER=enabled"),
    pat("P16", "fraud rules CPU explosion", ["fraud-service", "payments-api", "auth-service"],
        "risk evaluation deadline exceeded; rule graph evaluations increased tenfold",
        "A cyclic rule dependency caused repeated evaluation and saturated fraud-service CPU.",
        "Scaled payment pods", "Disabled the cyclic ruleset and restored the previous compiled graph",
        "riskctl ruleset activate stable"),
    pat("P17", "object storage permission drift", ["returns-service", "catalog-service", "recommendations-service"],
        "object storage access denied for workload identity; HTTP status 403",
        "A workload-identity policy update removed the service's required object-store permission.",
        "Rotated application secrets", "Restored the least-privilege workload policy binding",
        "iamctl bind --namespace {ns} --role object-reader"),
    pat("P18", "Kafka consumer lag", ["catalog-service", "orders-service", "inventory-service", "shipping-service"],
        "consumer lag above one million; partition processing time exceeds poll interval",
        "A poison message caused repeated processing while healthy partitions accumulated lag.",
        "Added consumers without isolating poison data", "Quarantined the poison message and resumed all partitions",
        "kafkactl quarantine --consumer {service} --offset current"),
    pat("P19", "gateway rate-limit misconfiguration", ["api-gateway", "auth-service", "checkout-web"],
        "local rate limit rejected normal traffic; remaining tokens zero unexpectedly",
        "A rate-limit configuration used a global bucket instead of a per-client bucket.",
        "Scaled upstream services", "Reverted to per-client buckets and reloaded gateway configuration",
        "gatewayctl ratelimit apply per-client-stable"),
    pat("P20", "connection pool leak", ["pricing-service", "shipping-service", "returns-service", "catalog-service"],
        "database connection leak detected; borrowed connection not returned by worker",
        "An exception path skipped transaction cleanup and slowly leaked database connections.",
        "Raised the maximum pool size", "Disabled the faulty worker and deployed guaranteed transaction cleanup",
        "kubectl -n {ns} set env deploy/{service} FAULTY_WORKER_ENABLED=false"),
    pat("P21", "clock skew authentication failure", ["auth-service", "api-gateway", "payments-api", "fraud-service"],
        "token rejected: issued-at time is in the future; node clock offset above threshold",
        "Time synchronization failed on one node pool, invalidating otherwise valid signed tokens.",
        "Rotated signing keys", "Cordon-drained skewed nodes and restored time synchronization",
        "kubectl cordon -l time-sync=unhealthy"),
    pat("P22", "thread pool starvation", ["payments-api", "orders-service", "inventory-service", "pricing-service"],
        "request executor queue full; active threads equal configured maximum",
        "Blocking dependency calls occupied every request thread because timeout limits were missing.",
        "Increased pod count only", "Applied dependency timeouts and isolated blocking work in a bounded pool",
        "kubectl -n {ns} set env deploy/{service} DEPENDENCY_TIMEOUT_MS=1500"),
    pat("P23", "ephemeral disk pressure", ["search-service", "catalog-service", "recommendations-service", "api-gateway"],
        "ephemeral storage usage above limit; node reports DiskPressure",
        "Verbose request logging filled ephemeral storage and triggered pod eviction.",
        "Restarted evicted pods", "Reduced log level, rotated files and expanded the bounded volume",
        "kubectl -n {ns} set env deploy/{service} LOG_LEVEL=info"),
    pat("P24", "retry storm", ["cart-service", "checkout-web", "api-gateway", "shipping-service"],
        "retry budget exhausted; concurrent requests rising while success rate falls",
        "Clients retried without jitter during a short dependency slowdown, creating a self-sustaining storm.",
        "Removed request timeouts", "Enabled exponential backoff, jitter and a strict retry budget",
        "kubectl -n {ns} set env deploy/{service} RETRY_BUDGET_PERCENT=10"),
    pat("P25", "model or rules artifact mismatch", ["recommendations-service", "fraud-service", "search-service"],
        "artifact checksum mismatch; runtime rejected incompatible feature schema",
        "The newly promoted artifact expected a feature schema newer than the serving runtime.",
        "Restarted serving pods", "Rolled back the artifact and pinned schema compatibility checks",
        "artifactctl promote last-known-good --service {service}"),
]

ONE_OFF_CAUSES = [
    ("configuration parser rejected a duplicated key", "Restored the last validated configuration"),
    ("a single availability-zone route was withdrawn", "Shifted traffic away from the affected zone"),
    ("an upstream quota was lowered without notice", "Applied an emergency quota increase"),
    ("a malformed partner payload triggered an unbounded parser path", "Quarantined the payload and bounded parsing"),
    ("inode exhaustion prevented temporary files from being created", "Removed orphaned files and added inode alerts"),
    ("a stale DNS record pointed at a retired endpoint", "Published the correct record and reduced DNS TTL"),
    ("a canary configuration referenced a missing secret", "Restored the secret reference and restarted the canary"),
    ("a maintenance lock was not released after timeout", "Cleared the orphaned lock and added lease expiry"),
    ("a malformed localization bundle crashed one worker path", "Rolled back the bundle and added schema validation"),
    ("a dependency returned compressed data with an unsupported encoding", "Disabled the encoding and upgraded the client"),
]

CASE_TARGETS = {
    "known-repeat": 26,
    "lookalike": 19,
    "cross-service": 14,
    "novel": 14,
    "stale-conflict": 10,
    "noisy-cascade": 9,
}


def read_json(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())


def write_json(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")


def generated(row: dict[str, Any]) -> bool:
    return row.get("data_provenance", {}).get("generator") == GENERATOR


def iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


def random_time(rng: random.Random, start: datetime, end: datetime) -> datetime:
    seconds = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randrange(seconds))


def log_bundle(
    rng: random.Random,
    record_id: str,
    service: str,
    fired_at: datetime,
    signature: str,
    *,
    test_case: bool = False,
    noisy: bool = False,
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    start = fired_at - timedelta(minutes=8)
    signal_slots = {9, 14, 19, 24}
    for i in range(30):
        ts = start + timedelta(seconds=i * 17 + rng.randrange(0, 8))
        request_id = f"req-{rng.randrange(10**9, 10**10 - 1)}"
        if i in signal_slots:
            level = "error" if i in {14, 24} else "warn"
            msg = signature
            event = "dependency_or_runtime_failure"
        elif noisy and i in {6, 12, 17, 27}:
            level = "warn"
            msg = rng.choice([
                "transient client disconnect; request cancelled",
                "health probe exceeded soft latency threshold",
                "background cache refresh missed one interval",
                "metrics exporter queue temporarily behind",
            ])
            event = "non_causal_noise"
        else:
            level = "info"
            msg = f"request completed status=200 latency_ms={rng.randrange(35, 480)}"
            event = "request_completed"
        payload = {
            "level": level,
            "ts": ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "service": service,
            "event": event,
            "msg": msg,
            "request_id": request_id,
            "record": record_id,
        }
        lines.append(json.dumps(payload, separators=(",", ":")))
    picks = [2, 5, 8, 9, 10, 13, 14, 15, 18, 19, 23, 24]
    if test_case:
        # Tests expose symptoms and stable error signatures, never the labelled root-cause sentence.
        picks = [4, 7, 9, 10, 12, 14, 15, 18, 19, 21, 24, 26]
    return lines, [lines[i] for i in picks]


def add_engineers() -> list[dict[str, Any]]:
    engineers = [e for e in read_json(DATA / "engineers.json") if not generated(e)]
    existing = {e["id"] for e in engineers}
    for engineer_id, name, role, team in NEW_ENGINEERS:
        if engineer_id in existing:
            continue
        engineers.append({
            "id": engineer_id,
            "name": name,
            "role": role,
            "team": team,
            "joined": "2023-04-03",
            "slack": "@" + name.lower().replace(" ", "."),
            "preference": "Wants concise evidence, a safe first action and a rollback command.",
            "data_provenance": {"kind": "synthetic", "generator": GENERATOR, "review_status": "generated"},
        })
    write_json(DATA / "engineers.json", engineers)
    return engineers


def add_deploys(rng: random.Random, engineers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deploys = [d for d in read_json(DATA / "deploys.json") if not generated(d)]
    needed = TARGET_DEPLOYS - len(deploys)
    start = datetime(2025, 10, 1, tzinfo=IST)
    end = datetime(2026, 9, 28, 23, 59, tzinfo=IST)
    next_id = max(int(d["id"].split("-")[1]) for d in deploys) + 1
    by_service_version: dict[str, int] = defaultdict(lambda: 1)
    people = {e["name"] for e in engineers if not e.get("left")}
    change_types = ["deploy", "config", "feature_flag", "certificate", "infrastructure"]
    summaries = [
        "Dependency and timeout hardening",
        "Sale readiness capacity update",
        "Schema compatibility checks",
        "Observability and alert tuning",
        "Cache invalidation reliability",
        "Routine security maintenance",
        "Worker concurrency adjustment",
        "Customer workflow improvements",
    ]
    additions = []
    services = list(SERVICE_INFO)
    for i in range(needed):
        service = services[i % len(services)]
        version_no = by_service_version[service]
        by_service_version[service] += 1
        owner = SERVICE_INFO[service][1]
        deployed_by = owner if owner in people else "Fatima Khan"
        additions.append({
            "id": f"DEP-{next_id + i}",
            "service": service,
            "version": f"v{2 + version_no // 20}.{version_no % 20}.0",
            "previous_version": f"v{2 + max(0, version_no - 1) // 20}.{max(0, version_no - 1) % 20}.0",
            "type": change_types[(i // len(services)) % len(change_types)],
            "at": iso(random_time(rng, start, end)),
            "deployed_by": deployed_by,
            "change_summary": summaries[i % len(summaries)],
            "environment": "production",
            "region": ["ap-south-1", "ap-south-2"][i % 2],
            "data_provenance": {"kind": "synthetic", "generator": GENERATOR, "review_status": "generated"},
        })
    deploys.extend(additions)
    deploys.sort(key=lambda d: d["at"])
    write_json(DATA / "deploys.json", deploys)
    return deploys


def nearest_deploy(deploys: list[dict[str, Any]], service: str, at: datetime) -> str | None:
    matches = [d for d in deploys if d["service"] == service and datetime.fromisoformat(d["at"]) <= at]
    return max(matches, key=lambda d: d["at"])["id"] if matches else None


def build_incident(
    rng: random.Random,
    incident_id: str,
    service: str,
    fire: datetime,
    pattern: dict[str, Any] | None,
    deploys: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    namespace, resolver, _ = SERVICE_INFO[service]
    if pattern:
        signature = pattern["signature"]
        root = pattern["root"]
        fix = pattern["fix"]
        failed_action = pattern["failed"]
        pattern_id = pattern["id"]
        title = f"{service} {pattern['name']} during production traffic"
        command = pattern["command"].format(ns=namespace, service=service)
    else:
        cause, fix = ONE_OFF_CAUSES[index % len(ONE_OFF_CAUSES)]
        component = ["worker", "route", "shard", "adapter", "scheduler"][index % 5]
        signature = f"isolated {component} fault token={incident_id.lower()} operation aborted"
        root = f"A one-off fault in {service}: {cause}."
        failed_action = "Restarted an unrelated healthy dependency"
        pattern_id = None
        title = f"{service} isolated {component} failure #{100 + index}"
        command = f"kubectl -n {namespace} rollout undo deploy/{service}"

    severity = ["SEV1", "SEV2", "SEV2", "SEV3"][index % 4]
    mttr = rng.randrange(14, 76)
    end = fire + timedelta(minutes=mttr)
    related_deploy = nearest_deploy(deploys, service, fire)
    full_logs, excerpt = log_bundle(rng, incident_id, service, fire, signature)
    log_path = DATA / "logs" / "generated" / f"{incident_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(full_logs) + "\n")

    failed_result = "The failure signature and customer error rate did not change."
    failure_outcome = "made_worse" if index % 9 == 0 else "did_not_work"
    impact_pct = 3 + index % 34
    knowledge_status = "superseded" if pattern and index % 29 == 0 else "current"
    final_fix = fix if knowledge_status == "current" else f"Temporary workaround: {fix}; superseded by the pattern's later permanent remediation."

    times = [
        fire + timedelta(minutes=2),
        fire + timedelta(minutes=max(4, mttr // 5)),
        fire + timedelta(minutes=max(6, mttr * 2 // 5)),
        fire + timedelta(minutes=max(8, mttr * 3 // 5)),
        fire + timedelta(minutes=max(10, mttr * 4 // 5)),
        end,
    ]
    timeline = [
        {"at": iso(times[0]), "actor": resolver, "action": "Acknowledged the page",
         "result": f"Started triage for {service}.", "outcome": "info", "command": None},
        {"at": iso(times[1]), "actor": resolver, "action": "Correlated alert metrics with logs",
         "result": f"Confirmed stable signature: {signature}.", "outcome": "info", "command": None},
        {"at": iso(times[2]), "actor": resolver, "action": failed_action,
         "result": failed_result, "outcome": failure_outcome, "command": None},
        {"at": iso(times[3]), "actor": "Fatima Khan", "action": "Isolated the failure mechanism",
         "result": root, "outcome": "info", "command": None},
        {"at": iso(times[4]), "actor": resolver, "action": final_fix,
         "result": "Error rate and latency returned to the pre-incident baseline.", "outcome": "worked", "command": command},
        {"at": iso(times[5]), "actor": resolver, "action": "Marked the incident resolved",
         "result": "Customer impact ended after a ten-minute verification window.", "outcome": "info", "command": None},
    ]
    action_item = f"Prevent recurrence of {pattern_id or 'isolated failure'} with an automated pre-production check ({service.upper()[:6]}-{500 + index})"
    customer_impact = f"Approximately {impact_pct}% of requests were slow or failed for {mttr} minutes in one production region."
    postmortem = (
        f"{title} - {service}, {severity}, {fire:%d %b %Y, %H:%M} IST. Impact: {customer_impact} "
        f"Root cause: {root} What did NOT work: {failed_action} ({failed_result}). "
        f"What fixed it: {final_fix}. Resolved by {resolver} in {mttr} minutes. Follow-ups: {action_item}."
    )
    return {
        "id": incident_id,
        "title": title,
        "date": iso(fire),
        "service": service,
        "severity": severity,
        "environment": "production",
        "region": ["ap-south-1", "ap-south-2"][index % 2],
        "alert": {
            "title": f"{service} error rate and latency outside SLO",
            "source": "Grafana Alerting",
            "service": service,
            "metric": f"service_requests_total{{service=\"{service}\"}} and request_duration_seconds",
            "condition": "error_ratio > 5% OR p95 > 2s for 5m",
            "value": f"errors {impact_pct}.2%, p95 {2 + index % 8}.4s",
            "fired_at": iso(fire),
            "dashboard_url": f"https://grafana.kartly.internal/d/{service}",
        },
        "log_excerpt": excerpt,
        "log_file": f"logs/generated/{incident_id}.log",
        "timeline": timeline,
        "failed_attempts": [{"action": failed_action, "result": failed_result}],
        "root_cause": root,
        "resolution": final_fix,
        "action_items": [action_item],
        "resolved_by": resolver,
        "responders": [resolver] if resolver == "Fatima Khan" else [resolver, "Fatima Khan"],
        "mttr_minutes": mttr,
        "customer_impact": customer_impact,
        "postmortem_summary": postmortem,
        "pattern_id": pattern_id,
        "knowledge_status": knowledge_status,
        "related_deploy_ids": [related_deploy] if related_deploy else [],
        "runbook_id": f"RB-{service}",
        "evidence": [
            {"id": f"E-{incident_id}-LOG", "kind": "logs", "path": f"logs/generated/{incident_id}.log", "redacted": True},
            {"id": f"E-{incident_id}-METRIC", "kind": "metric_snapshot", "window_minutes": 15, "redacted": True},
        ],
        "data_provenance": {
            "kind": "synthetic",
            "generator": GENERATOR,
            "seed": SEED,
            "schema_version": "1.0",
            "review_status": "generated-needs-domain-review",
            "split": "memory",
        },
    }


def add_incidents(rng: random.Random, deploys: list[dict[str, Any]]) -> list[dict[str, Any]]:
    incidents = [i for i in read_json(DATA / "incidents_history.json") if not generated(i)]
    needed = TARGET_INCIDENTS - len(incidents)
    services = list(SERVICE_INFO)
    start = datetime(2025, 10, 1, tzinfo=IST)
    end = datetime(2026, 9, 28, 23, 30, tzinfo=IST)
    next_number = max(int(i["id"].split("-")[1]) for i in incidents) + 1
    service_pattern_cursor: dict[str, int] = defaultdict(int)
    additions = []
    for index in range(needed):
        service = services[index % len(services)]
        one_off = index % 4 == 0
        pattern = None
        if not one_off:
            choices = [p for p in PATTERNS if service in p["services"]]
            cursor = service_pattern_cursor[service]
            pattern = choices[cursor % len(choices)]
            service_pattern_cursor[service] += 1
        incident_id = f"INC-{next_number + index}"
        fire = random_time(rng, start, end).replace(second=0, microsecond=0)
        additions.append(build_incident(rng, incident_id, service, fire, pattern, deploys, index))
    incidents.extend(additions)
    incidents.sort(key=lambda i: i["date"])
    write_json(DATA / "incidents_history.json", incidents)
    return incidents


def expected_ids(incidents: list[dict[str, Any]], pattern_id: str) -> list[str]:
    matches = [
        i for i in incidents
        if i.get("pattern_id") == pattern_id and i.get("knowledge_status", "current") == "current"
    ]
    return [i["id"] for i in sorted(matches, key=lambda i: i["date"], reverse=True)[:8]]


def build_scenario(
    rng: random.Random,
    number: int,
    case_type: str,
    incidents: list[dict[str, Any]],
) -> dict[str, Any]:
    scenario_id = f"EVAL-{number:03d}"
    fire = datetime(2026, 9, 30, 8, 0, tzinfo=IST) + timedelta(minutes=number * 9)
    noisy = case_type == "noisy-cascade"
    if case_type == "novel":
        service = list(SERVICE_INFO)[number % len(SERVICE_INFO)]
        pattern = None
        signature = f"novel allocator epoch mismatch code=NVM-{number:03d}; no historical signature"
        cause = f"A previously unseen allocator epoch defect affected {service}; no matching historical incident exists."
        expected = []
        avoid = ["claim a strong historical match", "execute a destructive command without validation"]
        recommend = ["state that memory has no strong match", "collect traces and escalate to the service owner"]
        evidence_term = signature
    else:
        pattern = PATTERNS[(number * 7) % len(PATTERNS)]
        if case_type == "cross-service":
            # Select the least represented compatible service to force mechanism transfer.
            counts = defaultdict(int)
            for incident in incidents:
                if incident.get("pattern_id") == pattern["id"]:
                    counts[incident["service"]] += 1
            service = min(pattern["services"], key=lambda s: counts[s])
        else:
            service = pattern["services"][number % len(pattern["services"])]
        signature = pattern["signature"]
        cause = pattern["root"]
        expected = expected_ids(incidents, pattern["id"])
        avoid = [pattern["failed"], "apply a fix from a superficially similar but different signature"]
        recommend = [pattern["fix"], "verify recovery against the alert metric before resolving"]
        evidence_term = pattern["signature"]
        if case_type == "lookalike":
            signature = "symptoms resemble a common outage; decisive evidence: " + signature
        elif case_type == "stale-conflict":
            signature += "; older workaround is marked superseded"
        elif noisy:
            signature += "; downstream services also emit non-causal warnings"

    full_logs, excerpt = log_bundle(rng, scenario_id, service, fire, signature, test_case=True, noisy=noisy)
    log_path = DATA / "logs" / "evaluation" / f"{scenario_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(full_logs) + "\n")
    severity = ["SEV1", "SEV2", "SEV2", "SEV3"][number % 4]
    purpose = {
        "known-repeat": "Measure retrieval and reuse of a proven remediation.",
        "lookalike": "Reject a superficial match and follow the decisive error signature.",
        "cross-service": "Transfer a failure mechanism safely across service boundaries.",
        "novel": "Abstain from claiming memory support for a genuinely new failure.",
        "stale-conflict": "Prefer current evidence over a superseded historical workaround.",
        "noisy-cascade": "Find the causal signal among correlated but non-causal warnings.",
    }[case_type]
    return {
        "id": scenario_id,
        "name": f"{case_type.replace('-', ' ').title()} {number:03d}",
        "purpose": purpose,
        "case_type": case_type,
        "featured": False,
        "service": service,
        "severity": severity,
        "alert": {
            "title": f"{service} customer request SLO violation",
            "source": "Grafana Alerting",
            "service": service,
            "metric": f"service_slo_violation{{service=\"{service}\"}}",
            "condition": "error_ratio > 5% OR p95 > 2s for 5m",
            "value": f"errors {7 + number % 24}.1%, p95 {3 + number % 7}.2s",
            "fired_at": iso(fire),
            "dashboard_url": f"https://grafana.kartly.internal/d/{service}",
        },
        "log_excerpt": excerpt,
        "log_file": f"logs/evaluation/{scenario_id}.log",
        "expected": {
            "pattern_id": pattern["id"] if pattern else None,
            "strong_match": pattern is not None,
            "likely_cause": cause,
            "should_avoid": avoid,
            "should_recommend": recommend,
            "notes": purpose,
            "expected_incident_ids": expected,
            "required_evidence_terms": [evidence_term],
            "expected_escalation": SERVICE_INFO[service][1] if pattern is None else None,
            "max_response_seconds": 25,
        },
        "data_provenance": {
            "kind": "synthetic",
            "generator": GENERATOR,
            "seed": SEED,
            "schema_version": "1.0",
            "review_status": "generated-needs-domain-review",
            "split": "evaluation",
        },
    }


def add_scenarios(rng: random.Random, incidents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scenarios = [s for s in read_json(DATA / "demo_scenarios.json") if not generated(s)]
    for scenario in scenarios:
        scenario.setdefault("featured", True)
        scenario.setdefault("case_type", "curated-demo")
        pattern_id = scenario.get("expected", {}).get("pattern_id")
        if pattern_id:
            # The original hand-authored evidence remains valid, while new
            # same-pattern memories are also acceptable retrieval results.
            original = scenario["expected"].get("expected_incident_ids", [])
            additions = expected_ids(incidents, pattern_id)
            scenario["expected"]["expected_incident_ids"] = list(dict.fromkeys(original + additions))
    needed = TARGET_SCENARIOS - len(scenarios)
    case_types: list[str] = []
    for case_type, count in CASE_TARGETS.items():
        case_types.extend([case_type] * count)
    if len(case_types) != needed:
        raise ValueError(f"case target count {len(case_types)} does not equal required additions {needed}")
    for offset, case_type in enumerate(case_types, start=1):
        scenarios.append(build_scenario(rng, offset, case_type, incidents))
    write_json(DATA / "demo_scenarios.json", scenarios)
    return scenarios


def add_runbooks() -> None:
    runbook_dir = DATA / "runbooks"
    runbook_dir.mkdir(parents=True, exist_ok=True)
    for service, (namespace, owner, team) in SERVICE_INFO.items():
        path = runbook_dir / f"{service}.md"
        if path.exists() and service in {
            "checkout-web", "payments-api", "orders-service", "auth-service",
            "search-service", "notification-service",
        }:
            continue
        path.write_text(
            f"# Runbook: {service}\n\n"
            f"**ID:** RB-{service} · **Owner:** {owner} ({team}) · **Namespace:** {namespace} · **Version:** 1.0\n\n"
            "## Safe triage\n"
            f"1. Open the {service} service dashboard and set the window to 30 minutes before the alert.\n"
            "2. Confirm the stable error signature in logs; do not infer cause from the alert title alone.\n"
            f"3. Inspect recent changes: `kubectl -n {namespace} rollout history deploy/{service}`.\n"
            "4. Compare dependency health and check whether impact is isolated to one region.\n\n"
            "## Change safety\n"
            "Every mutation requires an incident note, verification metric and rollback command. "
            "Destructive database, queue and cache commands require service-owner approval.\n\n"
            "## Escalation\n"
            f"Escalate to {owner}. If multiple services or infrastructure are affected, include Platform/SRE.\n"
        )


def main() -> None:
    rng = random.Random(SEED)
    engineers = add_engineers()
    deploys = add_deploys(rng, engineers)
    incidents = add_incidents(rng, deploys)
    scenarios = add_scenarios(rng, incidents)
    add_runbooks()
    print(f"Generated corpus: {len(incidents)} historical incidents, {len(scenarios)} held-out scenarios, "
          f"{len(deploys)} deploy/change records, {len(SERVICE_INFO)} services.")


if __name__ == "__main__":
    main()
