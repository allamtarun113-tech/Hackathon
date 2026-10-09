// Thin client for the PagerMind FastAPI backend. No service credentials ship in the browser bundle.

import type {
  ApiErrorBody,
  Engineer,
  FeedbackOutcome,
  FeedbackResponse,
  IncidentDetail,
  MemoryJob,
  MemoryJobDetail,
  ResolveResponse,
  UploadedFile,
} from "./types";

const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
    readonly requestId?: string | null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
    throw new ApiError(
      body?.error.message ?? `Request failed (${response.status})`,
      response.status,
      body?.error.code ?? "http_error",
      body?.error.request_id,
    );
  }
  return (await response.json()) as T;
}

const json = (body: unknown, operatorToken?: string): RequestInit => ({
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    ...(operatorToken ? { Authorization: `Bearer ${operatorToken}` } : {}),
  },
  body: JSON.stringify(body),
});

// The operator token is typed by the person running the demo and kept only in memory/sessionStorage.
// It is never part of the build, so it cannot leak through the JavaScript bundle.
const operatorHeaders = (operatorToken: string): RequestInit => ({
  headers: { Authorization: `Bearer ${operatorToken}` },
});

export const api = {
  engineers: () => request<Engineer[]>("/engineers"),

  uploadLog: (file: File, engineerId: string, conversationId?: string | null) => {
    const form = new FormData();
    form.append("file", file);
    form.append("engineer_id", engineerId);
    if (conversationId) form.append("conversation_id", conversationId);
    return request<UploadedFile>("/files", { method: "POST", body: form });
  },

  feedback: (body: {
    engineer_id: string;
    conversation_id: string;
    message_id: string;
    step_id?: string | null;
    rating?: 1 | -1 | null;
    outcome?: FeedbackOutcome | null;
    note?: string | null;
  }) => request<FeedbackResponse>("/feedback", json(body)),

  incident: (incidentId: string, operatorToken: string) =>
    request<IncidentDetail>(`/incidents/${encodeURIComponent(incidentId)}`, operatorHeaders(operatorToken)),

  resolveIncident: (
    incidentId: string,
    operatorToken: string,
    body: {
      engineer_id: string;
      root_cause: string;
      resolution: string;
      commands: string[];
      failed_actions: string[];
      follow_ups: string[];
      verified_feedback_ids: string[];
      rejected_feedback_ids: string[];
    },
  ) =>
    request<ResolveResponse>(
      `/incidents/${encodeURIComponent(incidentId)}/resolve`,
      json(body, operatorToken),
    ),

  memoryJob: (jobId: string) => request<MemoryJob>(`/memory-jobs/${encodeURIComponent(jobId)}`),

  /** Operator only: retains that ended without searchable memory (zero units, rejected, dead letter). */
  memoryJobsNeedingAttention: (operatorToken: string) =>
    request<MemoryJobDetail[]>("/memory-jobs", operatorHeaders(operatorToken)),
};

export interface StreamEvent {
  event: string;
  data: unknown;
}

/**
 * POST /chat and yield Server-Sent Events. EventSource cannot POST, so the stream is parsed from fetch.
 */
export async function* streamChat(
  body: { engineer_id: string; message: string; conversation_id?: string | null; file_ids: string[] },
  signal: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const response = await fetch(`${API_BASE}/chat`, { ...json(body), signal });
  if (!response.ok || !response.body) {
    const error = (await response.json().catch(() => null)) as ApiErrorBody | null;
    throw new ApiError(error?.error.message ?? "Chat request failed", response.status, error?.error.code ?? "http_error");
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const parsed = parseEvent(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (parsed) yield parsed;
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function parseEvent(block: string): StreamEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // keep-alive comment
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return null;
  try {
    return { event, data: JSON.parse(data.join("\n")) };
  } catch {
    return null;
  }
}
