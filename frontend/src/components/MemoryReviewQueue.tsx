"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import type { MemoryJobDetail } from "@/lib/types";

import { readToken, storeToken } from "./ResolveIncidentForm";

const STATUS_TONE: Record<string, string> = { needs_review: "warn", rejected: "warn", dead_letter: "danger" };

/**
 * Operator view of retains that produced no searchable memory. A silent empty retain makes it look as if
 * Hindsight knows something it does not, so these are surfaced instead of hidden in the outbox.
 */
export function MemoryReviewQueue() {
  const [token, setToken] = useState(readToken);
  const [jobs, setJobs] = useState<MemoryJobDetail[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      setJobs(await api.memoryJobsNeedingAttention(token));
      storeToken(token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <details className="card review-queue">
      <summary>
        <strong>Memory review queue</strong>
        <span className="muted small">operator · retains with no searchable memory</span>
      </summary>
      <div className="review-queue-controls">
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="Operator key"
          autoComplete="off"
          aria-label="Operator key"
        />
        <button type="button" className="secondary small" disabled={!token || busy} onClick={() => void load()}>
          {busy ? "Loading…" : jobs ? "Refresh" : "Load"}
        </button>
      </div>
      {error && <p className="error-text small">{error}</p>}
      {jobs && jobs.length === 0 && <p className="muted small">Every retain produced searchable memory.</p>}
      {jobs && jobs.length > 0 && (
        <ul className="review-jobs">
          {jobs.map((job) => (
            <li key={job.job_id}>
              <span className={`badge ${STATUS_TONE[job.status] ?? "neutral"}`}>{job.status.replace("_", " ")}</span>{" "}
              <span className="chip mono">{job.document_id}</span>
              <span className="muted small">
                {" "}
                {job.source_type} · attempt {job.attempt_count}/{job.max_attempts}
                {job.memory_unit_count != null && ` · ${job.memory_unit_count} units`}
              </span>
              {(job.rejection_reason || job.last_error) && (
                <div className="small">{job.rejection_reason ?? job.last_error}</div>
              )}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
