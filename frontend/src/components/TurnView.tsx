"use client";

import { useState } from "react";

import type { ChatTurn } from "@/hooks/useIncidentChat";
import type { FeedbackOutcome } from "@/lib/types";

import { BaselineCard } from "./BaselineCard";
import { EvidencePanel } from "./EvidencePanel";
import { ProgressTimeline } from "./ProgressTimeline";
import { RecommendationCard } from "./RecommendationCard";

interface Props {
  turn: ChatTurn;
  engineerId: string | null;
  conversationId: string | null;
  onFeedback?: (stepId: string, outcome: FeedbackOutcome | null) => void;
}

export function TurnView({ turn, engineerId, conversationId, onFeedback }: Props) {
  const [showSources, setShowSources] = useState(false);
  const [showBaseline, setShowBaseline] = useState(false);

  const streaming = turn.status === "streaming";
  const answered = Boolean(turn.result) || (streaming && !turn.clarification && !turn.rejection);

  const matchCount = turn.result?.recommendation.matched_incidents.length ?? 0;
  const memoryCount = turn.result?.memory_summary?.memories ?? 0;

  return (
    <article className="turn">
      <div className="bubble engineer">{turn.message}</div>
      <ProgressTimeline progress={turn.progress} streaming={streaming} />

      {turn.error && <p className="notice danger">{turn.error}</p>}
      {turn.rejection && <div className="bubble agent">{turn.rejection}</div>}
      {turn.clarification && <div className="bubble agent">{turn.clarification}</div>}

      {answered && (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {/* Main Answer: With Hindsight */}
          <RecommendationCard
            result={turn.result}
            pending={streaming}
            engineerId={engineerId}
            conversationId={conversationId}
            onFeedback={onFeedback}
          />

          {/* Action links: 1. View Sources  2. Without Memory */}
          {turn.result && !streaming && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 10,
                alignItems: "center",
                padding: "8px 12px",
                background: "var(--surface)",
                border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)",
              }}
            >
              <button
                type="button"
                className={`small ${showSources ? "" : "secondary"}`}
                onClick={() => setShowSources((prev) => !prev)}
                id="toggle-sources-btn"
              >
                📄 {showSources ? "Hide Sources" : "View Sources"}
                {matchCount > 0 && (
                  <span className="badge info" style={{ marginLeft: 4 }}>
                    {matchCount} matched
                  </span>
                )}
                {memoryCount > 0 && matchCount === 0 && (
                  <span className="badge neutral" style={{ marginLeft: 4 }}>
                    {memoryCount} facts
                  </span>
                )}
              </button>

              <button
                type="button"
                className={`small ${showBaseline ? "" : "secondary"}`}
                onClick={() => setShowBaseline((prev) => !prev)}
                id="toggle-baseline-btn"
              >
                🧠 {showBaseline ? "Hide Without Memory" : "Without Memory (Plain LLM)"}
              </button>

              <span className="muted small" style={{ marginLeft: "auto" }}>
                Click to inspect historical evidence or compare with raw LLM
              </span>
            </div>
          )}

          {/* Expandable Evidence / Sources */}
          {showSources && turn.result && (
            <div style={{ animation: "fadeIn 0.2s ease" }}>
              <EvidencePanel result={turn.result} />
            </div>
          )}

          {/* Expandable Without Memory */}
          {showBaseline && (
            <div style={{ animation: "fadeIn 0.2s ease" }}>
              <BaselineCard
                baseline={turn.baseline}
                pending={streaming}
                fallbackResult={turn.result}
                userMessage={turn.message}
              />
            </div>
          )}
        </div>
      )}
    </article>
  );
}
