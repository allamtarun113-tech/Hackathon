"use client";

import { useCallback, useReducer, useRef } from "react";

import { streamChat } from "@/lib/api";
import type {
  BaselineAnswer,
  ProgressEvent,
  RecentChange,
  RecommendationEvent,
} from "@/lib/types";

export interface ChatTurn {
  id: string;
  message: string;
  progress: ProgressEvent[];
  baseline: BaselineAnswer | null;
  recentChanges: RecentChange[];
  result: RecommendationEvent | null;
  clarification: string | null;
  rejection: string | null;
  error: string | null;
  status: "streaming" | "complete" | "failed";
}

interface State {
  conversationId: string | null;
  incidentId: string | null;
  turns: ChatTurn[];
  busy: boolean;
}

type Action =
  | { type: "start"; turn: ChatTurn }
  | { type: "event"; turnId: string; event: string; data: Record<string, unknown> }
  | { type: "fail"; turnId: string; message: string }
  | { type: "finish"; turnId: string }
  | { type: "reset" };

const initialState: State = { conversationId: null, incidentId: null, turns: [], busy: false };

function updateTurn(state: State, turnId: string, patch: (turn: ChatTurn) => Partial<ChatTurn>): State {
  return {
    ...state,
    turns: state.turns.map((turn) => (turn.id === turnId ? { ...turn, ...patch(turn) } : turn)),
  };
}

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case "reset":
      return initialState;
    case "start":
      return { ...state, busy: true, turns: [...state.turns, action.turn] };
    case "fail":
      return { ...updateTurn(state, action.turnId, () => ({ status: "failed", error: action.message })), busy: false };
    case "finish":
      return {
        ...updateTurn(state, action.turnId, (turn) => ({ status: turn.status === "failed" ? "failed" : "complete" })),
        busy: false,
      };
    case "event":
      return applyEvent(state, action.turnId, action.event, action.data);
  }
}

function applyEvent(state: State, turnId: string, event: string, data: Record<string, unknown>): State {
  switch (event) {
    case "conversation":
      return { ...state, conversationId: data.conversation_id as string };
    case "progress":
      return updateTurn(state, turnId, (turn) => {
        const progress = data as unknown as ProgressEvent;
        const others = turn.progress.filter((p) => p.node !== progress.node);
        return { progress: [...others, progress] };
      });
    case "baseline":
      return updateTurn(state, turnId, () => ({ baseline: data.baseline as BaselineAnswer }));
    case "recent_changes":
      return updateTurn(state, turnId, () => ({ recentChanges: data.changes as RecentChange[] }));
    case "recommendation": {
      const result = data as unknown as RecommendationEvent;
      return {
        ...updateTurn(state, turnId, () => ({ result })),
        incidentId: result.incident_id ?? state.incidentId,
      };
    }
    case "clarification":
      return updateTurn(state, turnId, () => ({ clarification: data.question as string }));
    case "rejected":
      return updateTurn(state, turnId, () => ({ rejection: data.reason as string }));
    case "error":
      return updateTurn(state, turnId, () => ({ status: "failed", error: data.message as string }));
    case "done":
      return { ...state, incidentId: (data.incident_id as string | null) ?? state.incidentId };
    default:
      return state;
  }
}

export function useIncidentChat(engineerId: string | null) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const controller = useRef<AbortController | null>(null);

  const send = useCallback(
    async (message: string, fileIds: string[], options: { newConversation?: boolean } = {}) => {
      if (!engineerId || state.busy) return;
      const conversationId = options.newConversation ? null : state.conversationId;
      if (options.newConversation) dispatch({ type: "reset" });
      const turnId = crypto.randomUUID();
      dispatch({
        type: "start",
        turn: {
          id: turnId,
          message,
          progress: [],
          baseline: null,
          recentChanges: [],
          result: null,
          clarification: null,
          rejection: null,
          error: null,
          status: "streaming",
        },
      });

      controller.current = new AbortController();
      try {
        for await (const { event, data } of streamChat(
          { engineer_id: engineerId, message, conversation_id: conversationId, file_ids: fileIds },
          controller.current.signal,
        )) {
          dispatch({ type: "event", turnId, event, data: data as Record<string, unknown> });
        }
      } catch (error) {
        if ((error as Error).name !== "AbortError") {
          dispatch({ type: "fail", turnId, message: (error as Error).message });
        }
      } finally {
        dispatch({ type: "finish", turnId });
      }
    },
    [engineerId, state.busy, state.conversationId],
  );

  const cancel = useCallback(() => controller.current?.abort(), []);
  const reset = useCallback(() => {
    controller.current?.abort();
    dispatch({ type: "reset" });
  }, []);

  return { ...state, send, cancel, reset };
}
