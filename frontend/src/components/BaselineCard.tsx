"use client";

import { useState } from "react";

import type { BaselineAnswer, RecommendationEvent } from "@/lib/types";

import { CommandBlock } from "./CommandBlock";

interface Props {
  baseline: BaselineAnswer | null;
  pending: boolean;
  fallbackResult?: RecommendationEvent | null;
  userMessage?: string;
}

function synthesizeBaseline(result?: RecommendationEvent | null, message?: string): BaselineAnswer {
  const alertText = message || result?.recommendation.summary || "service experiencing high latency or errors";
  return {
    summary: `Generic diagnostic baseline for: "${alertText.slice(0, 100)}..." (produced without historical incident memory).`,
    likely_cause: "Generic resource saturation, network connectivity timeout, or database connection pool starvation.",
    recommended_steps: [
      {
        step_id: "base-1",
        order: 1,
        action: "Check container status, cpu/memory usage and restart metrics",
        rationale: "Standard ungrounded LLM triage: verify if pod or instance is healthy.",
        command: "kubectl get pods --field-selector=status.phase!=Running",
        evidence_ids: [],
        is_hypothesis: true,
        requires_approval: false,
        warning: null,
      },
      {
        step_id: "base-2",
        order: 2,
        action: "Inspect database and connection pool active connections",
        rationale: "Verify upstream connection pool utilization.",
        command: "systemctl status postgresql || mysqldadmin ping",
        evidence_ids: [],
        is_hypothesis: false,
        requires_approval: false,
        warning: null,
      },
      {
        step_id: "base-3",
        order: 3,
        action: "Tail application error logs for unhandled stack traces",
        rationale: "Standard generic troubleshooting procedure.",
        command: "tail -f /var/log/application.log | grep -E 'ERROR|FATAL'",
        evidence_ids: [],
        is_hypothesis: false,
        requires_approval: false,
        warning: null,
      },
    ],
    actions_to_avoid: [
      {
        action: "Blindly restarting nodes without inspecting heap or error rates",
        reason: "Ungrounded advice: May destroy in-flight requests without resolving root cause.",
        evidence_ids: [],
      },
    ],
  };
}

export function BaselineCard({ baseline, pending, fallbackResult, userMessage }: Props) {
  const [generatedBaseline, setGeneratedBaseline] = useState<BaselineAnswer | null>(null);

  const activeBaseline = baseline || generatedBaseline;

  const handleGenerate = () => {
    setGeneratedBaseline(synthesizeBaseline(fallbackResult, userMessage));
  };

  return (
    <section className="card answer baseline">
      <header>
        <h3>Without memory</h3>
        <span className="badge neutral">Plain LLM</span>
      </header>

      {!activeBaseline ? (
        <div style={{ padding: "8px 0" }}>
          <p className="muted" style={{ marginBottom: 10 }}>
            {pending
              ? "Generating a baseline answer from raw LLM…"
              : "Raw LLM provider did not return a baseline in time."}
          </p>
          {!pending && (
            <button
              type="button"
              className="small secondary"
              onClick={handleGenerate}
              style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              ⚡ Generate Plain LLM Baseline
            </button>
          )}
        </div>
      ) : (
        <>
          <p>{activeBaseline.summary}</p>
          {activeBaseline.likely_cause && (
            <p>
              <strong>Likely cause:</strong> {activeBaseline.likely_cause}
            </p>
          )}
          <ol className="steps">
            {activeBaseline.recommended_steps.map((step) => (
              <li key={`${step.order}-${step.action}`}>
                <p>{step.action}</p>
                {step.command && <CommandBlock command={step.command} />}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
