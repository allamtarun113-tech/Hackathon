import type { MemoryFact, RecommendationEvent } from "@/lib/types";

const TYPE_BADGE: Record<string, { text: string; tone: string }> = {
  incident: { text: "incident", tone: "neutral" },
  feedback: { text: "feedback", tone: "info" },
  procedure: { text: "procedure", tone: "ok" },
  live_step: { text: "live step", tone: "info" },
};

function documentLabel(fact: MemoryFact): string {
  if (fact.incident_id) return fact.incident_id;
  if (fact.document_id?.startsWith("runbook:")) return fact.document_id.slice("runbook:".length);
  return fact.document_id ?? "Other memories";
}

/** Group by source document: one incident's postmortem and its verified feedback are separate documents. */
function groupByDocument(facts: MemoryFact[]): Map<string, MemoryFact[]> {
  const groups = new Map<string, MemoryFact[]>();
  for (const fact of facts) {
    const key = fact.document_id ?? documentLabel(fact);
    groups.set(key, [...(groups.get(key) ?? []), fact]);
  }
  return groups;
}

function TypeBadge({ type }: { type?: string | null }) {
  const badge = type ? TYPE_BADGE[type] : undefined;
  return badge ? <span className={`badge ${badge.tone}`}>{badge.text}</span> : null;
}

function FactMeta({ fact }: { fact: MemoryFact }) {
  const parts = [
    fact.occurred_at ? fact.occurred_at.slice(0, 10) : null,
    fact.score != null ? `score ${fact.score.toFixed(2)}` : null,
  ].filter(Boolean);
  return parts.length > 0 ? <span className="muted small"> · {parts.join(" · ")}</span> : null;
}

export function EvidencePanel({ result }: { result: RecommendationEvent }) {
  const matched = result.recommendation.matched_incidents;
  const isConsolidated = (fact: MemoryFact) => fact.fact_type === "observation" || fact.fact_type === "mental_model";
  const observations = result.evidence.filter(isConsolidated);
  const facts = result.evidence.filter((fact) => !isConsolidated(fact));
  const groups = groupByDocument(facts);

  return (
    <section className="card">
      <h3>Matched historical incidents</h3>
      {matched.length === 0 ? (
        <p className="muted">No past incident matched closely enough.</p>
      ) : (
        <ul className="matches">
          {matched.map((match) => (
            <li key={match.incident_id}>
              <span className="chip mono">{match.incident_id}</span> {match.why_similar}
            </li>
          ))}
        </ul>
      )}
      {result.recommendation.similarity_explanation && (
        <p className="muted small">{result.recommendation.similarity_explanation}</p>
      )}

      {observations.length > 0 && (
        <>
          <h3>What Hindsight has learned across incidents</h3>
          <p className="muted small">
            Patterns Hindsight consolidated from several incidents. Incidents they name count as evidence.
          </p>
          <ul className="observations">
            {observations.map((fact, index) => (
              <li key={fact.fact_id ?? `observation-${index}`}>
                <span className="badge info">{fact.fact_type === "mental_model" ? "mental model" : "observation"}</span>{" "}
                {fact.text}
                <FactMeta fact={fact} />
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Evidence used by Hindsight</h3>
      {groups.size === 0 ? (
        <p className="muted">No memory facts were used.</p>
      ) : (
        [...groups.entries()].map(([key, items]) => {
          const first = items[0]!;
          return (
            <details key={key} open={matched.some((m) => m.incident_id === first.incident_id)}>
              <summary>
                <span className="chip mono">{documentLabel(first)}</span>
                <TypeBadge type={first.memory_type} />
                <span className="muted small">{items.length} facts</span>
              </summary>
              <ul className="facts">
                {items.map((fact, index) => (
                  <li key={fact.fact_id ?? `${key}-${index}`}>
                    {fact.text}
                    <FactMeta fact={fact} />
                  </li>
                ))}
              </ul>
            </details>
          );
        })
      )}

      {result.error_signatures.length > 0 && (
        <>
          <h3>Error signatures</h3>
          <ul className="signatures">
            {result.error_signatures.map((signature) => (
              <li key={signature}>
                <code>{signature}</code>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
