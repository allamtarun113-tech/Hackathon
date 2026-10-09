"use client";

import { useState, type FormEvent } from "react";

import { api } from "@/lib/api";
import type { FeedbackRecord, ResolveResponse } from "@/lib/types";

import { MemoryJobStatus } from "./MemoryJobStatus";

export const TOKEN_KEY = "pagermind.operator";

type Review = "verify" | "reject" | "leave";

const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

export function readToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function storeToken(token: string) {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage unavailable: the token simply is not remembered for this tab.
  }
}

/**
 * Resolution is an operator action: it replaces canonical incident data and decides which
 * provisional feedback becomes memory. It requires the deployment's operator key.
 */
interface Props {
  incidentId: string;
  engineerId: string;
  onResolved?: (result: ResolveResponse) => void;
}

// Step outcomes are the learning signal; the resolver sees them pre-selected for verification and can change them.
const defaultReview = (item: FeedbackRecord): Review =>
  item.step_id && item.outcome && item.outcome !== "not_tried" ? "verify" : "leave";

export function ResolveIncidentForm({ incidentId, engineerId, onResolved }: Props) {
  const [token, setToken] = useState(readToken);
  const [feedback, setFeedback] = useState<FeedbackRecord[] | null>(null);
  const [reviews, setReviews] = useState<Record<string, Review>>({});
  const [rootCause, setRootCause] = useState("");
  const [resolution, setResolution] = useState("");
  const [commands, setCommands] = useState("");
  const [failed, setFailed] = useState("");
  const [followUps, setFollowUps] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ResolveResponse | null>(null);

  const loadFeedback = async () => {
    setBusy(true);
    setError(null);
    try {
      const incident = await api.incident(incidentId, token);
      storeToken(token);
      const pending = incident.feedback.filter((item) => item.validation_state === "pending");
      setFeedback(pending);
      setReviews(Object.fromEntries(pending.map((item) => [item.feedback_id, defaultReview(item)])));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const pick = (decision: Review) =>
      Object.entries(reviews)
        .filter(([, value]) => value === decision)
        .map(([id]) => id);
    try {
      const resolved = await api.resolveIncident(incidentId, token, {
          engineer_id: engineerId,
          root_cause: rootCause.trim(),
          resolution: resolution.trim(),
          commands: lines(commands),
          failed_actions: lines(failed),
          follow_ups: lines(followUps),
        verified_feedback_ids: pick("verify"),
        rejected_feedback_ids: pick("reject"),
      });
      setResult(resolved);
      onResolved?.(resolved);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    return (
      <section className="card">
        <h3>Incident {result.incident_id} resolved</h3>
        <p>
          Postmortem v{result.postmortem_version} saved. {result.verified_feedback} feedback item(s) verified,{" "}
          {result.rejected_feedback} rejected.
        </p>
        <MemoryJobStatus jobId={result.memory_job_id} label="Postmortem" />
        {result.feedback_memory_job_ids.map((jobId) => (
          <MemoryJobStatus key={jobId} jobId={jobId} label="Verified step outcome" />
        ))}
        <p className="muted small">Hindsight indexes asynchronously; new knowledge is available to later incidents.</p>
      </section>
    );
  }

  return (
    <form className="card resolve" onSubmit={(event) => void submit(event)}>
      <h3>
        Resolve <span className="mono">{incidentId}</span>
      </h3>

      <label>
        Operator key
        <input
          type="password"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          autoComplete="off"
          required
        />
      </label>

      {feedback === null ? (
        <button type="button" className="secondary" disabled={!token || busy} onClick={() => void loadFeedback()}>
          Load provisional feedback
        </button>
      ) : (
        <fieldset className="review">
          <legend>Provisional feedback</legend>
          {feedback.length === 0 && <p className="muted small">No provisional feedback on this incident.</p>}
          {feedback.map((item) => (
            <div key={item.feedback_id} className="review-row">
              <div>
                <strong>{item.outcome ?? "rating only"}</strong> on {item.step_id ?? "answer"} by{" "}
                {item.engineer_name ?? "unknown"}
                {item.note && <div className="muted small">{item.note}</div>}
              </div>
              <select
                value={reviews[item.feedback_id]}
                onChange={(event) =>
                  setReviews((current) => ({ ...current, [item.feedback_id]: event.target.value as Review }))
                }
              >
                <option value="leave">Leave provisional</option>
                <option value="verify">Verify → remember</option>
                <option value="reject">Reject</option>
              </select>
            </div>
          ))}
        </fieldset>
      )}

      <label>
        Confirmed root cause
        <textarea value={rootCause} onChange={(e) => setRootCause(e.target.value)} rows={2} required minLength={10} />
      </label>
      <label>
        What fixed it
        <textarea value={resolution} onChange={(e) => setResolution(e.target.value)} rows={2} required minLength={10} />
      </label>
      <label>
        Commands used <span className="muted small">(one per line)</span>
        <textarea value={commands} onChange={(e) => setCommands(e.target.value)} rows={2} />
      </label>
      <label>
        What did not help <span className="muted small">(one per line)</span>
        <textarea value={failed} onChange={(e) => setFailed(e.target.value)} rows={2} />
      </label>
      <label>
        Follow-ups <span className="muted small">(one per line)</span>
        <textarea value={followUps} onChange={(e) => setFollowUps(e.target.value)} rows={2} />
      </label>
      {error && <p className="error-text">{error}</p>}
      <button type="submit" disabled={busy || !token}>
        {busy ? "Saving…" : "Resolve and remember"}
      </button>
    </form>
  );
}
