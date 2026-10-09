// Shapes returned by the PagerMind API. Kept in sync with backend/app/agent/schemas.py and api/schemas.py.

export type MatchStrength = "strong" | "weak" | "none" | "unavailable";
export type FeedbackOutcome = "worked" | "partial" | "did_not_work" | "made_worse" | "not_tried";

export interface Engineer {
  engineer_id: string;
  display_name: string;
  title: string | null;
  team_name: string | null;
}

export interface MatchedIncident {
  incident_id: string;
  why_similar: string;
}

export interface RecommendedStep {
  step_id: string;
  order: number;
  action: string;
  rationale: string;
  command: string | null;
  evidence_ids: string[];
  is_hypothesis: boolean;
  requires_approval: boolean;
  warning: string | null;
}

export interface ActionToAvoid {
  action: string;
  reason: string;
  evidence_ids: string[];
}

export interface Recommendation {
  summary: string;
  is_strong_match: boolean;
  matched_incidents: MatchedIncident[];
  similarity_explanation: string;
  likely_cause: string;
  confidence: number;
  recommended_steps: RecommendedStep[];
  actions_to_avoid: ActionToAvoid[];
  escalation_team: string | null;
  abstention_reason: string | null;
}

export interface BaselineAnswer {
  summary: string;
  likely_cause: string;
  recommended_steps: RecommendedStep[];
  actions_to_avoid: ActionToAvoid[];
}

export type MemoryType = "incident" | "feedback" | "procedure" | "live_step";

export interface MemoryFact {
  fact_id: string | null;
  text: string;
  incident_id: string | null;
  document_id: string | null;
  occurred_at: string | null;
  tags: string[];
  score?: number | null;
  /**
   * "world" / "experience" are extracted facts; "observation" and "mental_model" are knowledge Hindsight
   * consolidated across incidents (they cite incidents by ID in their text, not by document).
   */
  fact_type?: string | null;
  memory_type?: MemoryType | null;
}

export interface MemorySummary {
  memories: number;
  incidents: number;
  observations: number;
  mental_models?: number;
  by_memory_type: Record<string, number>;
}

export interface RecentChange {
  deployment_id: string;
  service_id: string;
  version: string | null;
  change_type: string;
  change_summary: string | null;
  deployed_by: string | null;
  deployed_at: string;
}

export interface ProgressEvent {
  node: string;
  label: string;
  status: "running" | "done";
}

export interface RecommendationEvent {
  message_id: string;
  incident_id: string | null;
  recommendation: Recommendation;
  match_strength: MatchStrength;
  /** One sentence, tied to error signatures, on why this was a strong, weak or novel match. */
  match_explanation?: string | null;
  memory_summary?: MemorySummary | null;
  memory_available: boolean;
  evidence: MemoryFact[];
  retrieved_incident_ids: string[];
  error_signatures: string[];
  service_id: string | null;
}

export interface UploadedFile {
  file_id: string;
  sha256: string;
  size_bytes: number;
  mime_type: string;
  redaction_count: number;
  error_signatures: string[];
}

export interface FeedbackResponse {
  feedback_id: string;
  validation_state: "pending";
  retain_status: "provisional" | "not_eligible";
  reason: string;
}

export interface FeedbackRecord {
  feedback_id: string;
  step_id: string | null;
  outcome: FeedbackOutcome | null;
  rating: 1 | -1 | null;
  note: string | null;
  engineer_name: string | null;
  validation_state: "pending" | "verified" | "rejected" | "confirmed_by_resolution";
  retain_status: "not_eligible" | "queued" | "retained" | "failed";
  created_at: string;
}

export interface IncidentDetail {
  incident_id: string;
  title: string;
  status: string;
  service_id: string;
  feedback: FeedbackRecord[];
}

export interface ResolveResponse {
  incident_id: string;
  postmortem_version: number;
  memory_job_id: string;
  verified_feedback: number;
  rejected_feedback: number;
  feedback_memory_job_ids: string[];
}

export interface MemoryJob {
  job_id: string;
  status:
    | "pending"
    | "validating"
    | "submitted"
    | "processing"
    | "retained"
    | "needs_review"
    | "failed"
    | "rejected"
    | "dead_letter";
  attempt_count: number;
  max_attempts: number;
  memory_unit_count: number | null;
  created_at: string;
  submitted_at: string | null;
  completed_at: string | null;
}

/** Operator view of a memory job, including why it produced no searchable memory. */
export interface MemoryJobDetail extends MemoryJob {
  document_id: string;
  source_type: string;
  source_id: string;
  last_error: string | null;
  rejection_reason: string | null;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown>; request_id?: string | null };
}
