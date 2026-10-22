"use client";

import { useEffect, useRef, useState } from "react";

import { Composer } from "@/components/Composer";
import { EngineerPicker } from "@/components/EngineerPicker";
import { LearningLoop, replayMessage, type AlertSnapshot, type LoopProgress } from "@/components/LearningLoop";
import { ResolveIncidentForm } from "@/components/ResolveIncidentForm";
import { TurnView } from "@/components/TurnView";
import { useIncidentChat } from "@/hooks/useIncidentChat";

const EMPTY_LOOP: LoopProgress = { alertA: null, feedback: [], resolved: null };

export default function Workspace() {
  const [engineerId, setEngineerId] = useState<string | null>(null);
  const [loop, setLoop] = useState<LoopProgress>(EMPTY_LOOP);
  const chat = useIncidentChat(engineerId);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [chat.turns.length]);

  const firstAnswered = chat.turns.find((turn) => turn.result);
  const replayed = loop.alertA !== null;
  const liveAlert: AlertSnapshot | null = firstAnswered?.result
    ? {
        message: firstAnswered.message,
        incidentId: chat.incidentId,
        cited: firstAnswered.result.recommendation.matched_incidents.map((match) => match.incident_id),
        signatures: firstAnswered.result.error_signatures,
      }
    : null;
  const alertA = replayed ? loop.alertA : liveAlert;

  const replay = () => {
    if (!liveAlert) return;
    setLoop((current) => ({ ...current, alertA: liveAlert }));
    void chat.send(replayMessage(liveAlert), [], { newConversation: true });
  };

  const newIncident = () => {
    setLoop(EMPTY_LOOP);
    chat.reset();
  };

  return (
    <div className="workspace">
      <header className="topbar">
        <div className="brand">
          <div className="logo-icon">P</div>
          <div>
            <h1 style={{ fontSize: "15px", marginBottom: 0 }}>PagerMind</h1>
            <p className="muted small" style={{ fontSize: "11px", margin: 0 }}>AI-powered incident intelligence</p>
          </div>
        </div>
        <div className="topbar-actions">
          {chat.incidentId && (
            <div className="incident-chip">
              <span className="dot-live" />
              {chat.incidentId}
            </div>
          )}
          <EngineerPicker value={engineerId} onChange={setEngineerId} />
          <button
            id="new-incident-btn"
            type="button"
            className="secondary small"
            onClick={newIncident}
            disabled={chat.turns.length === 0}
          >
            + New
          </button>
        </div>
      </header>

      <main className="thread">
        {/* Workflow progress panel — shown only when there's an incident in progress */}
        {alertA && (
          <LearningLoop
            alertA={alertA}
            progress={loop}
            resultB={replayed ? (firstAnswered?.result ?? null) : null}
            replayed={replayed}
            busy={chat.busy}
            onReplay={replay}
          />
        )}

        {/* Empty / welcome state */}
        {chat.turns.length === 0 && (
          <div className="welcome-card">
            <div className="empty-icon">🔍</div>
            <h2>What's failing?</h2>
            <p>
              Select your role, then describe the incident below. PagerMind searches your team's
              past incidents and compares AI answers — with and without historical memory.
            </p>
            <div className="welcome-steps">
              <div className="welcome-step">
                <div className="welcome-step-num">1</div>
                Select your role
              </div>
              <div className="welcome-step">
                <div className="welcome-step-num">2</div>
                Describe the incident
              </div>
              <div className="welcome-step">
                <div className="welcome-step-num">3</div>
                Review AI recommendations
              </div>
              <div className="welcome-step">
                <div className="welcome-step-num">4</div>
                Resolve &amp; save to memory
              </div>
            </div>
          </div>
        )}

        {/* Chat turns */}
        {chat.turns.map((turn) => (
          <TurnView
            key={turn.id}
            turn={turn}
            engineerId={engineerId}
            conversationId={chat.conversationId}
            onFeedback={
              replayed
                ? undefined
                : (stepId, outcome) =>
                    setLoop((current) => ({ ...current, feedback: [...current.feedback, { stepId, outcome }] }))
            }
          />
        ))}

        {/* Resolve form — only once AI has answered and is no longer busy */}
        {chat.incidentId && engineerId && !chat.busy && chat.turns.some((t) => t.result) && (
          <>
            <div className="section-divider">Resolve Incident</div>
            <ResolveIncidentForm
              key={chat.incidentId}
              incidentId={chat.incidentId}
              engineerId={engineerId}
              onResolved={replayed ? undefined : (resolved) => setLoop((current) => ({ ...current, resolved }))}
            />
          </>
        )}

        <div ref={bottom} />
      </main>

      <footer className="composer-dock">
        <div className="composer-wrap">
          <Composer
            engineerId={engineerId}
            conversationId={chat.conversationId}
            busy={chat.busy}
            onSend={(message, fileIds) => void chat.send(message, fileIds)}
            onCancel={chat.cancel}
          />
        </div>
      </footer>
    </div>
  );
}
