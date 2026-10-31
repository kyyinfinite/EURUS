/** API contract types mirroring eurus.gateway.web.schemas (Python is the source of truth). */

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

/** Parsed form of the canonical `[attach id=… kind=… name=… mime=… size=…]` marker. */
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

/** Model input/output capability metadata (source of truth:
 *  eurus.providers.capabilities — conservative and honest). */
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

/* ---------- websocket payloads ---------- */

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
