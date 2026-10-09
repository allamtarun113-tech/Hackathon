import type { BaselineAnswer } from "@/lib/types";

import { CommandBlock } from "./CommandBlock";

export function BaselineCard({ baseline, pending }: { baseline: BaselineAnswer | null; pending: boolean }) {
  return (
    <section className="card answer baseline">
      <header>
        <h3>Without memory</h3>
        <span className="badge neutral">Plain LLM</span>
      </header>
      {!baseline ? (
        <p className="muted">{pending ? "Generating a baseline answer…" : "No baseline answer was produced."}</p>
      ) : (
        <>
          <p>{baseline.summary}</p>
          {baseline.likely_cause && (
            <p>
              <strong>Likely cause:</strong> {baseline.likely_cause}
            </p>
          )}
          <ol className="steps">
            {baseline.recommended_steps.map((step) => (
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
