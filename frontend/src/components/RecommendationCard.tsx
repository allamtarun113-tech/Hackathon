import type { FeedbackOutcome, MemorySummary, RecommendationEvent } from "@/lib/types";

import { CommandBlock } from "./CommandBlock";
import { StepFeedback } from "./StepFeedback";

const MATCH_LABELS = {
  strong: { text: "Strong historical match", tone: "ok" },
  weak: { text: "Weak match — appears new", tone: "warn" },
  none: { text: "No match — appears new", tone: "warn" },
  unavailable: { text: "Memory unavailable", tone: "danger" },
} as const;

interface Props {
  result: RecommendationEvent | null;
  pending: boolean;
  engineerId: string | null;
  conversationId: string | null;
  onFeedback?: (stepId: string, outcome: FeedbackOutcome | null) => void;
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

function MemoryUsage({ summary }: { summary: MemorySummary }) {
  if (summary.memories === 0) return <p className="memory-usage muted small">This answer used no memories.</p>;
  const types = Object.entries(summary.by_memory_type)
    .filter(([type]) => type !== "unknown")
    .map(([type, count]) => `${count} ${type}`);
  return (
    <p className="memory-usage small">
      This answer used <strong>{plural(summary.memories, "memory")}</strong> from{" "}
      <strong>{plural(summary.incidents, "incident")}</strong>
      {summary.observations > 0 && <> and {plural(summary.observations, "observation")}</>}
      {(summary.mental_models ?? 0) > 0 && <> and {plural(summary.mental_models ?? 0, "mental model")}</>}
      {types.length > 0 && <span className="muted"> · {types.join(", ")}</span>}
    </p>
  );
}

export function RecommendationCard({ result, pending, engineerId, conversationId, onFeedback }: Props) {
  if (!result) {
    return (
      <section className="card answer hindsight">
        <header>
          <h3>With Hindsight</h3>
        </header>
        <p className="muted">{pending ? "Reasoning over past incidents…" : "No recommendation was produced."}</p>
      </section>
    );
  }

  const { recommendation: rec, match_strength: strength } = result;
  const match = MATCH_LABELS[strength];
  const feedbackContext =
    engineerId && conversationId && result.message_id ? { engineerId, conversationId } : null;

  return (
    <section className="card answer hindsight">
      <header>
        <h3>With Hindsight</h3>
        <span className={`badge ${match.tone}`}>{match.text}</span>
        <span className="muted small">confidence {Math.round(rec.confidence * 100)}%</span>
      </header>
      {result.memory_summary && <MemoryUsage summary={result.memory_summary} />}
      {result.match_explanation && <p className="match-why small">{result.match_explanation}</p>}

      {!result.memory_available && (
        <p className="notice danger">
          Hindsight memory was unavailable. This answer comes from the language model alone.
        </p>
      )}
      {rec.abstention_reason && <p className="notice warn">{rec.abstention_reason}</p>}

      <p>{rec.summary}</p>
      {rec.likely_cause && (
        <p>
          <strong>Likely cause:</strong> {rec.likely_cause}
        </p>
      )}

      <h4>Recommended actions</h4>
      <ol className="steps">
        {rec.recommended_steps.map((step) => (
          <li key={step.step_id}>
            <p>
              {step.action}{" "}
              {step.is_hypothesis && <span className="badge neutral">hypothesis</span>}
              {step.requires_approval && <span className="badge danger">needs approval</span>}
            </p>
            {step.rationale && <p className="muted small">{step.rationale}</p>}
            {step.warning && <p className="notice danger small">{step.warning}</p>}
            {step.command && <CommandBlock command={step.command} />}
            {step.evidence_ids.length > 0 && (
              <p className="small">
                Evidence:{" "}
                {step.evidence_ids.map((id) => (
                  <span key={id} className="chip mono">
                    {id}
                  </span>
                ))}
              </p>
            )}
            {feedbackContext && (
              <StepFeedback
                engineerId={feedbackContext.engineerId}
                conversationId={feedbackContext.conversationId}
                messageId={result.message_id}
                stepId={step.step_id}
                onSaved={(outcome) => onFeedback?.(step.step_id, outcome)}
              />
            )}
          </li>
        ))}
      </ol>

      {rec.actions_to_avoid.length > 0 && (
        <>
          <h4>Avoid</h4>
          <ul className="avoid">
            {rec.actions_to_avoid.map((item) => (
              <li key={item.action}>
                <strong>{item.action}</strong> — {item.reason}
                {item.evidence_ids.map((id) => (
                  <span key={id} className="chip mono">
                    {id}
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </>
      )}

      {rec.escalation_team && (
        <p className="small">
          <strong>Escalate to:</strong> {rec.escalation_team}
        </p>
      )}
    </section>
  );
}
