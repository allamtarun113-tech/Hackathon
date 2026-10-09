import type { RecentChange } from "@/lib/types";

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function RecentChanges({ changes }: { changes: RecentChange[] }) {
  return (
    <section className="card">
      <h3>Recent deployments</h3>
      {changes.length === 0 ? (
        <p className="muted">No changes to this service or its dependencies in the last 24 hours.</p>
      ) : (
        <ul className="changes">
          {changes.map((change) => (
            <li key={change.deployment_id}>
              <span className="chip mono">{change.deployment_id}</span>
              <strong>{change.service_id}</strong> {change.version && <code>{change.version}</code>}{" "}
              <span className="badge neutral">{change.change_type}</span>
              <div className="muted small">
                {change.change_summary} · {change.deployed_by ?? "unknown"} ·{" "}
                {timeFormat.format(new Date(change.deployed_at))}
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">Correlation in time is not a confirmed cause.</p>
    </section>
  );
}
