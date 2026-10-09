"use client";

import { useEffect, useRef, useState } from "react";

import { Composer } from "@/components/Composer";
import { EngineerPicker } from "@/components/EngineerPicker";
import { LearningLoop, replayMessage, type AlertSnapshot, type LoopProgress } from "@/components/LearningLoop";
import { MemoryReviewQueue } from "@/components/MemoryReviewQueue";
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

  // Before the replay, alert A is whatever the live conversation answered first; after it, the frozen snapshot.
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
          <span className="logo" aria-hidden>
            ◎
          </span>
          <div>
            <h1>PagerMind</h1>
            <p className="muted small">Shared on-call workspace with incident memory</p>
          </div>
        </div>
        <div className="topbar-actions">
          {chat.incidentId && <span className="chip mono">{chat.incidentId}</span>}
          <EngineerPicker value={engineerId} onChange={setEngineerId} />
          <button type="button" className="secondary" onClick={newIncident} disabled={chat.turns.length === 0}>
            New incident
          </button>
        </div>
      </header>

      <main className="thread">
        <LearningLoop
          alertA={alertA}
          progress={loop}
          resultB={replayed ? (firstAnswered?.result ?? null) : null}
          replayed={replayed}
          busy={chat.busy}
          onReplay={replay}
        />

        {chat.turns.length === 0 && (
          <section className="empty card">
            <h2>What is failing?</h2>
            <p className="muted">
              Describe the alert and paste the key error lines, or attach a log file. PagerMind compares a plain
              model answer with one grounded in your team&apos;s past incidents, shows the evidence, and learns from
              your feedback and resolutions.
            </p>
          </section>
        )}

        {chat.turns.map((turn) => (
          <TurnView
            key={turn.id}
            turn={turn}
            engineerId={engineerId}
            conversationId={chat.conversationId}
            onFeedback={
              replayed
                ? undefined
                : (stepId, outcome) => setLoop((current) => ({ ...current, feedback: [...current.feedback, { stepId, outcome }] }))
            }
          />
        ))}

        {chat.incidentId && engineerId && !chat.busy && (
          <ResolveIncidentForm
            key={chat.incidentId}
            incidentId={chat.incidentId}
            engineerId={engineerId}
            onResolved={replayed ? undefined : (resolved) => setLoop((current) => ({ ...current, resolved }))}
          />
        )}
        <MemoryReviewQueue />
        <div ref={bottom} />
      </main>

      <footer className="composer-dock">
        <Composer
          engineerId={engineerId}
          conversationId={chat.conversationId}
          busy={chat.busy}
          onSend={(message, fileIds) => void chat.send(message, fileIds)}
          onCancel={chat.cancel}
        />
      </footer>
    </div>
  );
}
