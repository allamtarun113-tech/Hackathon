"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import type { FeedbackOutcome, FeedbackResponse } from "@/lib/types";

const MORE_OUTCOMES: { value: FeedbackOutcome; label: string }[] = [
  { value: "partial", label: "Partly helped" },
  { value: "made_worse", label: "Made it worse" },
  { value: "not_tried", label: "Not tried" },
];

const OUTCOME_TEXT: Record<FeedbackOutcome, string> = {
  worked: "Worked",
  did_not_work: "Did not work",
  partial: "Partly helped",
  made_worse: "Made it worse",
  not_tried: "Not tried",
};

interface Props {
  engineerId: string;
  conversationId: string;
  messageId: string;
  stepId: string;
  onSaved?: (outcome: FeedbackOutcome | null, response: FeedbackResponse) => void;
}

/**
 * The outcome of a recommended step is what the next incident learns from. One click records it as
 * provisional; it becomes Hindsight memory only once the resolver verifies it.
 */
export function StepFeedback({ engineerId, conversationId, messageId, stepId, onSaved }: Props) {
  const [note, setNote] = useState("");
  const [more, setMore] = useState(false);
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [saved, setSaved] = useState<{ outcome: FeedbackOutcome | null; response: FeedbackResponse } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (outcome: FeedbackOutcome | null, rating: 1 | -1 | null = null) => {
    setState("saving");
    setError(null);
    try {
      const response = await api.feedback({
        engineer_id: engineerId,
        conversation_id: conversationId,
        message_id: messageId,
        step_id: stepId,
        rating,
        outcome,
        note: note.trim() || null,
      });
      setSaved({ outcome, response });
      setState("saved");
      onSaved?.(outcome, response);
    } catch (err) {
      setError((err as Error).message);
      setState("error");
    }
  };

  if (state === "saved" && saved) {
    const provisional = saved.response.retain_status === "provisional";
    return (
      <div className="feedback saved">
        <span className={`badge ${saved.outcome === "worked" ? "ok" : saved.outcome ? "warn" : "neutral"}`}>
          {saved.outcome ? OUTCOME_TEXT[saved.outcome] : "Rated"}
        </span>
        <span className={`badge ${provisional ? "info" : "neutral"}`}>
          {provisional ? "Provisional — verify on resolve to remember" : "Saved, not remembered"}
        </span>
      </div>
    );
  }

  const busy = state === "saving";
  return (
    <div className="feedback">
      <button type="button" className="small outcome-worked" disabled={busy} onClick={() => void submit("worked", 1)}>
        ✓ Worked
      </button>
      <button
        type="button"
        className="small secondary outcome-failed"
        disabled={busy}
        onClick={() => void submit("did_not_work", -1)}
      >
        ✗ Didn&apos;t work
      </button>
      <input
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Optional: what happened? e.g. error rate dropped within 4 minutes"
        maxLength={1000}
      />
      {more ? (
        MORE_OUTCOMES.map((option) => (
          <button
            key={option.value}
            type="button"
            className="small secondary"
            disabled={busy}
            onClick={() => void submit(option.value)}
          >
            {option.label}
          </button>
        ))
      ) : (
        <button type="button" className="link small" onClick={() => setMore(true)}>
          More outcomes
        </button>
      )}
      {error && <span className="error-text small">{error}</span>}
    </div>
  );
}
