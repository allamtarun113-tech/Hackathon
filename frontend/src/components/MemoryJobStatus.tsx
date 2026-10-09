"use client";

import { useMemoryJob } from "@/hooks/useMemoryJob";
import type { MemoryJob } from "@/lib/types";

const STAGES = [
  { key: "queued", label: "Queued" },
  { key: "submitted", label: "Sent to Hindsight" },
  { key: "retained", label: "Retained" },
] as const;

const STAGE_OF: Record<MemoryJob["status"], number> = {
  pending: 0,
  validating: 0,
  failed: 0,
  submitted: 1,
  processing: 1,
  retained: 2,
  needs_review: 2,
  rejected: 2,
  dead_letter: 2,
};

const PROBLEMS: Partial<Record<MemoryJob["status"], string>> = {
  needs_review: "Hindsight extracted no facts — needs review, not searchable",
  rejected: "Rejected by memory guardrails",
  dead_letter: "Failed permanently",
  failed: "Retrying",
};

/** Watch one retain job move queued → submitted → retained. Hindsight indexes asynchronously. */
export function MemoryJobStatus({ jobId, label }: { jobId: string; label?: string }) {
  const { job, error } = useMemoryJob(jobId);
  return <RetainStepper job={job} error={error} label={label} />;
}

export function RetainStepper({ job, error, label }: { job: MemoryJob | null; error?: string | null; label?: string }) {
  if (error) return <span className="badge warn">Memory status unavailable</span>;
  if (!job) return <span className="badge neutral">Checking memory status…</span>;

  const stage = STAGE_OF[job.status] ?? 0;
  const problem = PROBLEMS[job.status];
  const done = job.status === "retained";

  return (
    <div className="retain-job" title={`${job.job_id} · attempt ${job.attempt_count}/${job.max_attempts}`}>
      {label && <span className="small">{label}</span>}
      <ol className="stepper" aria-label="Retain progress">
        {STAGES.map((item, index) => {
          const state =
            index < stage || done ? "done" : index === stage ? (problem && index === 2 ? "problem" : "active") : "todo";
          return (
            <li key={item.key} className={state}>
              <span className="dot" aria-hidden />
              {index === 2 && problem && job.status !== "failed" ? "Not retained" : item.label}
            </li>
          );
        })}
      </ol>
      {done && job.memory_unit_count != null && (
        <span className="badge ok">{job.memory_unit_count} facts searchable</span>
      )}
      {problem && <span className={`badge ${job.status === "failed" ? "warn" : "danger"}`}>{problem}</span>}
    </div>
  );
}
