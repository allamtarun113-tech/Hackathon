import type { ProgressEvent } from "@/lib/types";

interface Props {
  progress: ProgressEvent[];
  streaming: boolean;
}

/**
 * Inline pipeline bar shown below the user query while the workflow runs.
 * Collapses to a single "N steps done" line once complete.
 */
export function ProgressTimeline({ progress, streaming }: Props) {
  if (progress.length === 0) return null;

  const lastIndex = progress.length - 1;
  const allDone = !streaming;

  if (allDone && progress.length > 0) {
    // Compact "done" summary
    return (
      <div className="pipeline-bar" style={{ padding: "6px 12px" }}>
        <span className="pipeline-step done">
          <span className="pipeline-dot" style={{ background: "var(--ok)" }} />
        </span>
        <span style={{ fontSize: "12px", color: "var(--text-muted)" }}>
          {progress.length} pipeline step{progress.length === 1 ? "" : "s"} completed
        </span>
        <details style={{ marginLeft: "auto", fontSize: "12px" }}>
          <summary
            className="pipeline-toggle"
            style={{ listStyle: "none", cursor: "pointer" }}
          >
            View steps
          </summary>
          <div style={{ marginTop: 6, display: "flex", flexWrap: "wrap", gap: "4px 12px" }}>
            {progress.map((step) => (
              <span key={step.node} className="pipeline-step done" style={{ fontSize: "11px", color: "var(--ok)" }}>
                <span className="pipeline-dot" style={{ background: "var(--ok)" }} />
                {step.label}
              </span>
            ))}
          </div>
        </details>
      </div>
    );
  }

  // Running state — show all steps with current active one highlighted
  return (
    <div className="pipeline-bar" aria-live="polite">
      <div className="pipeline-steps">
        {progress.map((step, index) => {
          const active = streaming && index === lastIndex && step.status === "running";
          const done = !active && (index < lastIndex || !streaming);
          const state = active ? "active" : done ? "done" : "pending";
          return (
            <span key={step.node} className={`pipeline-step ${state}`}>
              <span className="pipeline-dot" />
              <span style={{ fontSize: "12px" }}>{step.label}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}
