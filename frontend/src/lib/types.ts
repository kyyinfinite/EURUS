
export type Role = "system" | "user" | "assistant" | "tool";

export interface ToolCallView {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface MessageView {
  sequence: number;
  role: Role;
  content: string;
  tool_calls: ToolCallView[];
  tool_call_id: string | null;
  name: string | null;
}

export type MediaKind = "image" | "audio" | "video" | "document";

export interface MediaView {
  id: string;
  session_id: string;
  filename: string;
  mime: string;
  kind: MediaKind;
  size: number;
  sha256: string;
  created_at: string;
  url: string;
}

export interface AttachmentRef {
  id: string;
  kind: MediaKind;
  name: string;
  mime: string;
  size: number;
}

export interface SessionView {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  active_run_id: string | null;
}

export interface RunView {
  run_id: string;
  session_id: string | null;
  objective: string;
  status: string;
  stop_reason: string | null;
  owner_id: string;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
  origin: "web" | "external";
}

export interface RuntimeEventView {
  event_id: string;
  run_id: string;
  event_type: string;
  sequence_no: number;
  payload: Record<string, unknown>;
  created_at: string;
  step_id: string | null;
  attempt_id: string | null;
  parent_run_id: string | null;
}

export interface ApprovalView {
  token: string;
  run_id: string;
  session_id: string;
  tool_name: string;
  arguments_json: string;
  display_notice?: string;
  fingerprint: string;
  expires_in_seconds: number;
}

export interface ModelCapabilities {
  textInput: boolean;
  imageInput: boolean;
  audioInput: boolean;
  videoInput: boolean;
  fileInput: boolean;
  textOutput: boolean;
  imageOutput: boolean;
  audioOutput: boolean;
  videoOutput: boolean;
  toolCalling: boolean;
  streaming: boolean;
}

export interface ProviderView {
  name: string;
  protocol: string;
  base_url: string;
  api_key_env: string;
  status: "ready" | "missing key" | "no key needed";
  builtin: boolean;
  capabilities?: ModelCapabilities;
  multimodal?: boolean;
}

export interface ModelInfoView {
  id: string;
  label: string;
  context: number;
}

export interface ToolView {
  name: string;
  description: string;
  mutates: boolean;
  requires_approval: boolean;
  parallel_safe: boolean;
  parameters: Record<string, unknown>;
}

export interface MCPServerView {
  name: string;
  transport: string;
  enabled: boolean;
  connected: boolean;
  tools: number;
  state: string;
  error: string;
}

export interface MemoryView {
  id: number;
  content: string;
  tags: string;
  category: string;
  created_at: string;
  updated_at: string;
  memory_type: string;
  confidence: number;
  source: string;
  use_count: number;
  last_used_at: string | null;
  scope: string;
  trust: string;
  expires_at: string | null;
  provenance?: unknown;
}

export interface ConfigView {
  config: Record<string, unknown>;
  config_path: string;
  revision: number;
  editable_sections: string[];
}

export interface ConfigPatchResponse {
  applied: boolean;
  revision: number;
  live_reload: boolean;
  restart_required: boolean;
  reload_error: string | null;
}

export interface LogRecordView {
  sequence: number;
  timestamp: number;
  level: string;
  logger: string;
  message: string;
}

export interface RuntimeHealth {
  started?: boolean;
  active_runs?: number;
  cached_sessions?: number;
  session_locks?: number;
  subscribers?: number;
  dropped_events?: number;
  closed?: boolean;
}

export interface RuntimeStatusView {
  active_runs: RunView[];
  recent_runs: RunView[];
  runtime_health: RuntimeHealth;
  pending_approvals: number;
  session_count: number;
  websocket_clients: number;
}

export interface DiagnosticsView {
  version: string;
  python: string;
  home_dir: string;
  config_path: string;
  config_exists: boolean;
  database_path: string;
  database_size_bytes: number;
  uptime_seconds: number;
  providers: { name: string; protocol: string; ready: boolean }[];
  mcp: {
    servers?: unknown[];
    warnings?: string[];
    policy_file?: string;
    google?: unknown;
  };
  sandbox_backend: string;
  workspace: string;
}

export interface DashboardView {
  runtime: RuntimeStatusView;
  recent_sessions: SessionView[];
  session_events: Record<string, number>;
  diagnostics: DiagnosticsView;
}

export interface HealthView {
  status: "ok";
  version: string;
  uptime_seconds: number;
  runtime: RuntimeHealth;
}


export interface AgentEventPayload {
  kind:
    | "step"
    | "text"
    | "tool_call"
    | "tool_result"
    | "done"
    | "lesson"
    | "skill"
    | "plan"
    | "recovery"
    | "model_call"
    | "text_reset";
  step: number;
  text?: string;
  tool_call?: ToolCallView;
  tool_result?: {
    call_id: string;
    name: string;
    output: string;
    is_error: boolean;
    error_code: string;
  };
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_tokens: number;
    cache_read_tokens: number;
  };
  stop_reason?: string;
}

export type GatewayEvent =
  | {
      type: "agent_event";
      session_id: string;
      run_id: string;
      origin: "web" | "external";
      sequence: number;
      event: AgentEventPayload;
    }
  | {
      type: "run_event";
      session_id: string;
      run_id: string;
      origin: "external";
      runtime_event: {
        event_id: string;
        event_type: string;
        sequence_no: number;
        payload: Record<string, unknown>;
        created_at: string;
        step_id: string | null;
        attempt_id: string | null;
      };
    }
  | {
      type: "run_status";
      session_id: string;
      run_id: string;
      status: string;
      stop_reason: string | null;
      origin: "web" | "external";
    }
  | ({ type: "approval_required" } & ApprovalView)
  | { type: "approval_resolved"; session_id: string; token: string; decision: string }
  | { type: "sessions_changed" }
  | { type: "hello"; version: string; clients: number }
  | { type: "subscribed" | "unsubscribed"; sessions: string[] }
  | { type: "pong"; time: number }
  | { type: "error"; detail: string };

export interface ApiErrorBody {
  error: { code: string; detail: string };
}


export interface MissionView {
  mission_id: string;
  owner_id: string;
  objective: string;
  status: string;
  root_run_id: string | null;
  plan: { route?: string; task_count?: number } & Record<string, unknown>;
  acceptance_criteria: string[];
  budget: Record<string, unknown>;
  version: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface MissionTaskView {
  task_id: string;
  mission_id: string;
  parent_task_id: string | null;
  task_key: string;
  title: string;
  objective: string;
  depends_on: string[];
  required_capabilities: string[];
  preferred_role: string | null;
  input_contract: Record<string, unknown>;
  output_contract: Record<string, unknown>;
  acceptance_criteria: string[];
  status: string;
  priority: number;
  attempt: number;
  assignment_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface MissionAssignmentView {
  assignment_id: string;
  role: string;
  status: string;
  task_id: string | null;
}

export interface MissionAggregation {
  total: number;
  succeeded: number;
  failed: number;
  pending: number;
  cancelled: number;
  complete: boolean;
}

export interface MissionDetail {
  mission: MissionView;
  tasks: MissionTaskView[];
  assignments: MissionAssignmentView[];
  aggregation: MissionAggregation;
}

export interface MissionArtifactView {
  artifact_id: string;
  owner_id: string;
  mission_id: string;
  task_id: string | null;
  assignment_id: string | null;
  artifact_type: string;
  media_type: string;
  content_hash: string;
  size_bytes: number;
  version: number;
  metadata: Record<string, unknown>;
  allowed_recipients: string[];
  created_at: string;
}

export interface ArtifactView extends MissionArtifactView {
  name: string;
  source_tool: string;
  run_id: string;
  expires_at: string | null;
}

export interface ArtifactPage {
  items: ArtifactView[];
  next_cursor: string | null;
  total: number;
}

export interface MissionLessonView {
  lesson_id: string;
  text: string;
  mission_id?: string;
}

export interface MissionRoleStats {
  role: string;
  tasks: number;
  succeeded: number;
  failed: number;
  blocked: number;
  revisions: number;
}

export interface MissionAnalyticsView {
  missions_total: number;
  missions_learned: number;
  decisions: Record<string, number>;
  expired: number;
  dimension_means: Record<string, number>;
  roles: MissionRoleStats[];
  lessons: MissionLessonView[];
}

export interface MissionEvaluationView {
  dimension: string;
  score: number;
  passed: boolean;
  evidence: string[];
  created_at: string;
}

export interface MissionLearningView {
  mission_id: string;
  status: string;
  objective: string;
  learned: boolean;
  evaluations: MissionEvaluationView[];
  lessons: MissionLessonView[];
}

export interface MissionVerificationView {
  check_name: string;
  status: string;
  category: string;
  command_argv: string[];
  working_directory: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  stdout_tail: string;
  stderr_tail: string;
  environment: Record<string, unknown>;
  mission_id: string;
  task_id: string;
  artifact_id: string | null;
}

export interface MissionFindingView {
  finding_id: string;
  severity: string;
  category: string;
  location: string;
  evidence: string;
  suggested_remediation: string;
  disposition: string;
  revision_task_id: string | null;
}

export interface MissionReviewView {
  review_id: string;
  mission_id: string;
  task_id: string;
  reviewer_assignment_id: string | null;
  status: string;
  findings: MissionFindingView[];
  summary: string;
  cycle: number;
  created_at: string;
}

export interface AcceptanceDecisionView {
  mission_id: string;
  decision: string;
  reasons: string[];
  blocking_finding_ids: string[];
  failed_check_names: string[];
  unavailable_check_names: string[];
  evaluated_at: string;
}
