"use client";

import { useState, type FormEvent } from "react";

import { api } from "@/lib/api";
import type { FeedbackRecord, ResolveResponse } from "@/lib/types";

import { MemoryJobStatus } from "./MemoryJobStatus";

export const TOKEN_KEY = "pagermind.operator";

export const DEFAULT_DEMO_TOKEN =
  process.env.NEXT_PUBLIC_OPERATOR_TOKEN ||
  "8907e39c0d76dc68d37fdec580e37573144f7e7365bee0148a3e364b890fd888";

export function readToken(): string {
  try {
    return window.sessionStorage.getItem(TOKEN_KEY) || DEFAULT_DEMO_TOKEN;
  } catch {
    return DEFAULT_DEMO_TOKEN;
  }
}

export function storeToken(token: string) {
  try {
    window.sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    // Storage unavailable
  }
}

type Review = "verify" | "reject" | "leave";

const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

interface Props {
  incidentId: string;
  engineerId: string;
  onResolved?: (result: ResolveResponse) => void;
}

const defaultReview = (item: FeedbackRecord): Review =>
  item.step_id && item.outcome && item.outcome !== "not_tried" ? "verify" : "leave";

export function ResolveIncidentForm({ incidentId, engineerId, onResolved }: Props) {
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
      const incident = await api.incident(incidentId, DEFAULT_DEMO_TOKEN);
      const pending = incident.feedback.filter((item) => item.validation_state === "pending");
      setFeedback(pending);
      setReviews(Object.fromEntries(pending.map((item) => [item.feedback_id, defaultReview(item)])));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();

    const trimmedRoot = rootCause.trim();
    const trimmedRes = resolution.trim();

    if (!trimmedRoot || !trimmedRes) {
      setError("Both Root Cause and Resolution are required.");
      return;
    }

    if (trimmedRoot.length < 10) {
      setError(`Root Cause is too short (${trimmedRoot.length}/10 chars minimum). Please enter at least 10 characters.`);
      return;
    }

    if (trimmedRes.length < 10) {
      setError(`Resolution is too short (${trimmedRes.length}/10 chars minimum). Please enter at least 10 characters.`);
      return;
    }

    setBusy(true);
    setError(null);

    const pick = (decision: Review) =>
      Object.entries(reviews)
        .filter(([, value]) => value === decision)
        .map(([id]) => id);

    try {
      const resolved = await api.resolveIncident(incidentId, DEFAULT_DEMO_TOKEN, {
        engineer_id: engineerId,
        root_cause: trimmedRoot,
        resolution: trimmedRes,
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
      <section className="card" style={{ borderLeft: "4px solid var(--ok)", padding: "18px 20px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 8 }}>
          <span style={{ fontSize: 20 }}>✅</span>
          <h3 style={{ margin: 0, fontSize: 16 }}>Incident {result.incident_id} Saved to Database</h3>
          <span className="badge ok" style={{ marginLeft: "auto" }}>Resolved &amp; Retained</span>
        </div>
        <p style={{ color: "var(--text-muted)", fontSize: 13, marginBottom: 14 }}>
          Postmortem stored in knowledge base. {result.verified_feedback} step outcome(s) verified for future recall.
        </p>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <MemoryJobStatus jobId={result.memory_job_id} label="Postmortem Knowledge Unit" />
          {result.feedback_memory_job_ids.map((jobId) => (
            <MemoryJobStatus key={jobId} jobId={jobId} label="Verified step outcome" />
          ))}
        </div>
      </section>
    );
  }

  return (
    <form className="card resolve-form" onSubmit={(event) => void submit(event)}>
      <div className="resolve-form-header">
        <span style={{ fontSize: 18 }}>💾</span>
        <div>
          <h3 style={{ margin: 0, fontSize: 15 }}>Save Incident to Database</h3>
          <p className="muted small" style={{ margin: 0 }}>
            Store postmortem in institutional memory for <span className="mono">{incidentId}</span>
          </p>
        </div>
      </div>

      {/* Provisional Feedback Review (if loaded) */}
      {feedback !== null && (
        <div className="feedback-review">
          <div className="feedback-review-header">Review engineer step ratings</div>
          {feedback.length === 0 ? (
            <p className="muted small" style={{ padding: "8px 12px", margin: 0 }}>
              No provisional feedback pending on this incident.
            </p>
          ) : (
            feedback.map((item) => (
              <div key={item.feedback_id} className="feedback-review-item">
                <div style={{ fontSize: 13 }}>
                  <strong>{item.outcome ?? "rating"}</strong> on {item.step_id ?? "step"}
                  {item.note && <span className="muted"> — {item.note}</span>}
                </div>
                <select
                  value={reviews[item.feedback_id]}
                  onChange={(event) =>
                    setReviews((current) => ({ ...current, [item.feedback_id]: event.target.value as Review }))
                  }
                >
                  <option value="verify">✓ Verify (Save to memory)</option>
                  <option value="leave">Leave provisional</option>
                  <option value="reject">Reject</option>
                </select>
              </div>
            ))
          )}
        </div>
      )}

      <div className="resolve-row">
        <label className="resolve-label">
          Confirmed Root Cause * <span className="muted small">(min. 10 chars)</span>
          <textarea
            value={rootCause}
            onChange={(e) => setRootCause(e.target.value)}
            placeholder="Describe the verified root cause (e.g. database pool exhaustion caused by high connection acquisition timeout)"
            rows={2}
            required
            minLength={10}
          />
        </label>

        <label className="resolve-label">
          What Fixed It / Resolution * <span className="muted small">(min. 10 chars)</span>
          <textarea
            value={resolution}
            onChange={(e) => setResolution(e.target.value)}
            placeholder="Describe what fixed it (e.g. restarted affected instances and increased max_connections pool size)"
            rows={2}
            required
            minLength={10}
          />
        </label>
      </div>

      <details style={{ marginTop: 2 }}>
        <summary className="muted small" style={{ cursor: "pointer", fontWeight: 500 }}>
          + Add commands, failed attempts, or follow-ups (optional)
        </summary>
        <div style={{ display: "grid", gap: 10, marginTop: 8 }}>
          <label className="resolve-label">
            Commands used <span>(one per line)</span>
            <textarea
              value={commands}
              onChange={(e) => setCommands(e.target.value)}
              placeholder="kubectl rollout restart deployment/payments-api&#10;systemctl restart postgresql"
              rows={2}
            />
          </label>
          <label className="resolve-label">
            What did not help <span>(helps AI warn future engineers)</span>
            <textarea
              value={failed}
              onChange={(e) => setFailed(e.target.value)}
              placeholder="Restarting the load balancer did not reduce error rates"
              rows={2}
            />
          </label>
          <label className="resolve-label">
            Follow-up actions <span>(one per line)</span>
            <textarea
              value={followUps}
              onChange={(e) => setFollowUps(e.target.value)}
              placeholder="Tune HikariCP connection timeout in staging&#10;Add p99 alert for pool queue length"
              rows={2}
            />
          </label>
        </div>
      </details>

      {/* Error notification and dedicated retry button */}
      {error && (
        <div
          className="notice danger"
          style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, marginTop: 4 }}
        >
          <div style={{ fontSize: 13 }}>
            <strong>Error:</strong> {error}
          </div>
          <button
            type="button"
            className="danger-btn small"
            onClick={() => void submit()}
            disabled={busy}
          >
            🔄 Retry Adding to DB
          </button>
        </div>
      )}

      {/* Actions */}
      <div className="resolve-actions">
        <button
          id="save-incident-db-btn"
          type="submit"
          disabled={busy || !rootCause.trim() || !resolution.trim()}
        >
          {busy ? "Saving to Database…" : "💾 Save Incident to Database"}
        </button>

        {feedback === null && (
          <button
            type="button"
            className="secondary small"
            disabled={busy}
            onClick={() => void loadFeedback()}
            title="Load ratings given by engineers during troubleshooting"
          >
            Review Step Ratings
          </button>
        )}
      </div>
    </form>
  );
}
