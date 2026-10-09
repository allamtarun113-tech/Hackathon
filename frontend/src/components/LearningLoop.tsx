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

/**
 * Alert B must carry the same evidence as alert A. Attachments belong to A's conversation and cannot be
 * re-sent, so the signatures extracted from them are pasted into the replayed message instead.
 */
export function replayMessage(alert: AlertSnapshot): string {
  const missing = alert.signatures.filter((signature) => !alert.message.includes(signature));
  if (missing.length === 0) return alert.message;
  return `${alert.message}\n\nError lines:\n${missing.join("\n")}`;
}

export interface LoopProgress {
  /** Alert A, frozen when it is replayed so the comparison survives the new conversation. */
  alertA: AlertSnapshot | null;
  feedback: { stepId: string; outcome: FeedbackOutcome | null }[];
  resolved: ResolveResponse | null;
}

interface Props {
  /** Alert A while it is still the live conversation, otherwise the frozen snapshot. */
  alertA: AlertSnapshot | null;
  progress: LoopProgress;
  /** The answer to the replayed alert, once it arrives. */
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
        <strong>{title}</strong>
        {children && <div className="small">{children}</div>}
      </div>
    </li>
  );
}

const ids = (values: string[]) =>
  values.length === 0 ? <span className="muted">none</span> : values.map((id) => <span key={id} className="chip mono">{id}</span>);

/**
 * The product in four steps: Hindsight cites the past → an engineer records what worked → resolving
 * retains it → the same alert later cites this incident. Every step reads real API state.
 */
export function LearningLoop({ alertA, progress, resultB, replayed, busy, onReplay }: Props) {
  const jobId = progress.resolved?.memory_job_id ?? null;
  const { job, error } = useMemoryJob(jobId);
  const retained = job?.status === "retained";
  const retainFailed = job != null && ["needs_review", "rejected", "dead_letter"].includes(job.status);

  const outcomes = progress.feedback.filter((item) => item.outcome && item.outcome !== "not_tried");
  const incidentA = alertA?.incidentId ?? null;
  const citedB = resultB?.recommendation.matched_incidents.map((match) => match.incident_id) ?? [];
  const learned = incidentA != null && citedB.includes(incidentA);
  const retrievedOnly = !learned && incidentA != null && (resultB?.retrieved_incident_ids ?? []).includes(incidentA);
  const feedbackUsedB = (resultB?.evidence ?? []).filter(
    (fact) => fact.incident_id === incidentA && fact.memory_type === "feedback",
  ).length;

  const step1: StepState = alertA ? "done" : "active";
  const step2: StepState = !alertA ? "todo" : outcomes.length > 0 ? "done" : "active";
  const step3: StepState = !alertA ? "todo" : retained ? "done" : retainFailed ? "problem" : progress.resolved ? "active" : outcomes.length > 0 ? "active" : "todo";
  const step4: StepState = !retained ? "todo" : !resultB ? "active" : learned ? "done" : "problem";

  return (
    <section className="card learning-loop" aria-label="Hindsight learning loop">
      <header>
        <h3>Hindsight learning loop</h3>
        <span className="muted small">cite → learn → retain → re-cite</span>
      </header>
      <ol className="loop-steps">
        <Step index={1} state={step1} title="Alert A: Hindsight cites past incidents">
          {alertA ? <>Cited {ids(alertA.cited)}{incidentA && <> · opened {ids([incidentA])}</>}</> : "Describe an alert below to start."}
        </Step>
        <Step index={2} state={step2} title="Engineer marks a step worked / did not work">
          {outcomes.length > 0
            ? `${outcomes.length} step outcome${outcomes.length === 1 ? "" : "s"} recorded (provisional until verified).`
            : alertA && "Use ✓ Worked or ✗ Didn't work on a recommended step."}
        </Step>
        <Step index={3} state={step3} title="Resolve: the postmortem is retained by Hindsight">
          {progress.resolved ? (
            <>
              <RetainStepper job={job} error={error} />
              {progress.resolved.feedback_memory_job_ids.length > 0 && (
                <span className="muted">
                  + {progress.resolved.feedback_memory_job_ids.length} verified step outcome(s) queued for memory.
                </span>
              )}
            </>
          ) : (
            alertA && "Resolve the incident below; verified step outcomes are retained with the postmortem."
          )}
        </Step>
        <Step index={4} state={step4} title="Alert B (same signature) cites this incident">
          {!retained && "Available once the postmortem is retained."}
          {retained && !replayed && (
            <button type="button" disabled={busy || !alertA} onClick={onReplay}>
              Replay alert A as a new incident
            </button>
          )}
          {replayed && !resultB && "Hindsight is reasoning over the updated memory…"}
          {resultB && (
            <>
              Alert B cited {ids(citedB)}.{" "}
              {learned ? (
                <span className="badge ok">Learned: {incidentA} is now evidence</span>
              ) : retrievedOnly ? (
                <span className="badge warn">{incidentA} was retrieved but not cited as a match</span>
              ) : (
                <span className="badge warn">{incidentA} was not retrieved yet</span>
              )}
              {feedbackUsedB > 0 && <span className="muted"> · used {feedbackUsedB} verified step outcome(s)</span>}
            </>
          )}
        </Step>
      </ol>
    </section>
  );
}
