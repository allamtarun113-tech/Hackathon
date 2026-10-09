import type { ChatTurn } from "@/hooks/useIncidentChat";
import type { FeedbackOutcome } from "@/lib/types";

import { BaselineCard } from "./BaselineCard";
import { EvidencePanel } from "./EvidencePanel";
import { ProgressTimeline } from "./ProgressTimeline";
import { RecentChanges } from "./RecentChanges";
import { RecommendationCard } from "./RecommendationCard";

interface Props {
  turn: ChatTurn;
  engineerId: string | null;
  conversationId: string | null;
  onFeedback?: (stepId: string, outcome: FeedbackOutcome | null) => void;
}

export function TurnView({ turn, engineerId, conversationId, onFeedback }: Props) {
  const streaming = turn.status === "streaming";
  const answered = Boolean(turn.result) || (streaming && !turn.clarification && !turn.rejection);

  return (
    <article className="turn">
      <div className="bubble engineer">{turn.message}</div>
      <ProgressTimeline progress={turn.progress} streaming={streaming} />

      {turn.error && <p className="notice danger">{turn.error}</p>}
      {turn.rejection && <div className="bubble agent">{turn.rejection}</div>}
      {turn.clarification && <div className="bubble agent">{turn.clarification}</div>}

      {answered && (
        <>
          <div className="comparison">
            <BaselineCard baseline={turn.baseline} pending={streaming} />
            <RecommendationCard
              result={turn.result}
              pending={streaming}
              engineerId={engineerId}
              conversationId={conversationId}
              onFeedback={onFeedback}
            />
          </div>
          <div className="context-grid">
            {turn.result && <EvidencePanel result={turn.result} />}
            {(turn.recentChanges.length > 0 || turn.result) && <RecentChanges changes={turn.recentChanges} />}
          </div>
        </>
      )}
    </article>
  );
}
