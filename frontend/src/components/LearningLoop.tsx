"use client";

import { useMemoryJob } from "@/hooks/useMemoryJob";
import type { FeedbackOutcome, RecommendationEvent, ResolveResponse } from "@/lib/types";

import { RetainStepper } from "./MemoryJobStatus";

export interface AlertSnapshot {
  message: string;
  incidentId: string | null;
  cited: string[];
  /** Signatures PagerMind extracted for alert A, including any that came from an attached log. */
  signatures: string[];
}

export function replayMessage(alert: AlertSnapshot): string {
  const missing = alert.signatures.filter((signature) => !alert.message.includes(signature));
  if (missing.length === 0) return alert.message;
  return `${alert.message}\n\nError lines:\n${missing.join("\n")}`;
}

export interface LoopProgress {
  alertA: AlertSnapshot | null;
  feedback: { stepId: string; outcome: FeedbackOutcome | null }[];
  resolved: ResolveResponse | null;
}

interface Props {
  alertA: AlertSnapshot | null;
  progress: LoopProgress;
  resultB: RecommendationEvent | null;
  replayed: boolean;
  busy: boolean;
  onReplay: () => void;
}

type StepState = "done" | "active" | "todo" | "problem";

function Step({ index, state, title, children }: { index: number; state: StepState; title: string; children?: React.ReactNode }) {
  return (
    <li className={`loop-step ${state}`}>
      <span className="loop-index" aria-hidden>
        {state === "done" ? "✓" : index}
      </span>
      <div>
        <strong style={{ fontSize: "13px" }}>{title}</strong>
        {children && <div className="step-body">{children}</div>}
      </div>
    </li>
  );
}

const ids = (values: string[]) =>
  values.length === 0 ? <span className="muted">none</span> : values.map((id) => <span key={id} className="chip mono">{id}</span>);

export function LearningLoop({ alertA, progress, replayed, busy, onReplay }: Props) {
  const jobId = progress.resolved?.memory_job_id ?? null;
  const { job, error } = useMemoryJob(jobId);
  const retained = job?.status === "retained";
  const retainFailed = job != null && ["needs_review", "rejected", "dead_letter"].includes(job.status);

  const outcomes = progress.feedback.filter((item) => item.outcome && item.outcome !== "not_tried");
  const incidentA = alertA?.incidentId ?? null;

  const step1: StepState = alertA ? "done" : "active";
  const step2: StepState = !alertA ? "todo" : outcomes.length > 0 ? "done" : "active";
  const step3: StepState = !alertA ? "todo" : retained ? "done" : retainFailed ? "problem" : progress.resolved ? "active" : outcomes.length > 0 ? "active" : "todo";

  return (
    <section className="card learning-loop" aria-label="Incident Memory Lifecycle">
      <div className="loop-header">
        <h3>⚡ Continuous Incident Learning Loop</h3>
        <span className="muted small">Recall → Learn → Retain</span>
      </div>

      <ol className="loop-steps">
        <Step index={1} state={step1} title="Historical Retrieval">
          {alertA ? <>Cited {ids(alertA.cited)}{incidentA && <> · Incident {ids([incidentA])}</>}</> : "Awaiting incident query…"}
        </Step>

        <Step index={2} state={step2} title="Action Verification">
          {outcomes.length > 0
            ? `${outcomes.length} action outcome(s) recorded.`
            : alertA && "Mark recommended steps as ✓ Worked or ✗ Didn't work."}
        </Step>

        <Step index={3} state={step3} title="Database & Memory Retention">
          {progress.resolved ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <RetainStepper job={job} error={error} />
              {retained && !replayed && (
                <button
                  type="button"
                  className="small"
                  style={{ alignSelf: "flex-start", marginTop: 4 }}
                  disabled={busy || !alertA}
                  onClick={onReplay}
                >
                  🔄 Test Recall on Similar Alert
                </button>
              )}
            </div>
          ) : (
            alertA && "Save the postmortem below to update institutional memory."
          )}
        </Step>
      </ol>
    </section>
  );
}
