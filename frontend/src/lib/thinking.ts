import type { AgentEventPayload } from "./types";

export interface RuntimeActivity {
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface ActivityItem {
  id: string;
  title: string;
  detail: string;
  tone: "neutral" | "success" | "warning" | "danger";
  source: "agent" | "runtime";
}

function textValue(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

export function describeCurrentActivity(events: AgentEventPayload[]): string {
  const latest = [...events].reverse().find((event) => event.kind !== "text" && event.kind !== "text_reset");
  if (!latest) return "Preparing your request";
  if (latest.kind === "tool_call" && latest.tool_call) return `Using ${latest.tool_call.name}`;
  if (latest.kind === "tool_result" && latest.tool_result) {
    return latest.tool_result.is_error ? `Recovering from ${latest.tool_result.name}` : `Finished ${latest.tool_result.name}`;
  }
  if (latest.kind === "plan") return "Updating the execution plan";
  if (latest.kind === "recovery") return "Recovering and adjusting the approach";
  if (latest.kind === "lesson") return "Applying learned context";
  if (latest.kind === "skill") return "Activating a specialist skill";
  if (latest.kind === "model_call") return "Processing model response";
  if (latest.kind === "media_input_analyzing") {
    return latest.media_kind ? `Analyzing ${latest.media_kind} input` : "Analyzing media input";
  }
  if (latest.kind === "content_block_started" || latest.kind === "content_block_delta") {
    const blockType = typeof latest.block?.type === "string" ? String(latest.block.type) : "content";
    return `Composing a ${blockType} response block`;
  }
  if (latest.kind === "audio_output_started") return "Preparing a spoken reply";
  if (latest.kind === "artifact") {
    return latest.artifact_status === "failed" ? "Artifact generation failed" : "Preparing an artifact";
  }
  if (latest.kind === "step") return `Working through step ${latest.step}`;
  if (latest.kind === "done") return "Finishing the response";
  return "Thinking through your request";
}

export function buildActivityTimeline(
  events: AgentEventPayload[],
  runtimeEvents: RuntimeActivity[],
): ActivityItem[] {
  const items: ActivityItem[] = [];
  events.forEach((event, index) => {
    if (event.kind === "text" || event.kind === "text_reset" || event.kind === "done") return;
    if (event.kind === "tool_call" && event.tool_call) {
      items.push({ id: `agent-${index}`, title: "Tool started", detail: event.tool_call.name, tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "tool_result" && event.tool_result) {
      items.push({ id: `agent-${index}`, title: event.tool_result.is_error ? "Tool needs attention" : "Tool completed", detail: event.tool_result.name, tone: event.tool_result.is_error ? "danger" : "success", source: "agent" });
      return;
    }
    if (event.kind === "media_input_analyzing") {
      items.push({ id: `agent-${index}`, title: "Media analysis", detail: event.media_kind ? `Analyzing ${event.media_kind} attachment` : "Analyzing attached media", tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "content_block_started" || event.kind === "content_block_completed") {
      const blockType = typeof event.block?.type === "string" ? String(event.block.type) : "content";
      items.push({ id: `agent-${index}`, title: event.kind === "content_block_started" ? "Response block started" : "Response block completed", detail: `${blockType} block`, tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "audio_output_started") {
      items.push({ id: `agent-${index}`, title: "Voice reply", detail: "Synthesizing a spoken reply", tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "artifact" && event.artifact_id) {
      items.push({ id: `agent-${index}`, title: event.artifact_status === "failed" ? "Artifact failed" : "Artifact ready", detail: event.artifact_status === "failed" ? "The artifact could not be produced" : `Artifact ${event.artifact_id.slice(0, 10)}`, tone: event.artifact_status === "failed" ? "danger" : "success", source: "agent" });
      return;
    }
    if (event.kind === "plan") {
      items.push({ id: `agent-${index}`, title: "Plan updated", detail: textValue(event.text) || `Step ${event.step}`, tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "recovery") {
      items.push({ id: `agent-${index}`, title: "Recovery attempt", detail: textValue(event.text) || "Adjusting the approach", tone: "warning", source: "agent" });
      return;
    }
    if (event.kind === "step") {
      items.push({ id: `agent-${index}`, title: "Reasoning step", detail: `Step ${event.step}`, tone: "neutral", source: "agent" });
      return;
    }
    if (event.kind === "lesson" || event.kind === "skill") {
      items.push({ id: `agent-${index}`, title: event.kind === "lesson" ? "Context applied" : "Skill activated", detail: textValue(event.text), tone: "success", source: "agent" });
      return;
    }
    if (event.kind === "model_call") {
      const tokens = (event.usage?.input_tokens ?? 0) + (event.usage?.output_tokens ?? 0);
      items.push({ id: `agent-${index}`, title: "Model response received", detail: tokens > 0 ? `${tokens.toLocaleString()} tokens` : "Response received", tone: "success", source: "agent" });
    }
  });

  runtimeEvents.forEach((event, index) => {
    const payload = event.payload;
    const eventType = event.event_type;
    if (eventType === "run.started" || eventType === "plan.updated" || eventType === "tool.call.started" || eventType === "tool.call.finished" || eventType === "recovery.attempted") return;
    if (eventType === "media.generation.started") {
      items.push({ id: `runtime-${index}`, title: "Media generation", detail: textValue(payload.kind) === "video" ? "Generating a video" : "Generating an image", tone: "neutral", source: "runtime" });
      return;
    }
    if (eventType === "media.generation.ready") {
      items.push({ id: `runtime-${index}`, title: "Media generated", detail: textValue(payload.kind) === "video" ? "The generated video is ready" : "The generated image is ready", tone: "success", source: "runtime" });
      return;
    }
    if (eventType === "media.generation.failed") {
      items.push({ id: `runtime-${index}`, title: "Media generation failed", detail: textValue(payload.error) || "The provider could not produce the media", tone: "danger", source: "runtime" });
      return;
    }
    if (eventType === "media.input.analyzing") {
      items.push({ id: `runtime-${index}`, title: "Media analysis", detail: textValue(payload.media_kind) ? `Analyzing ${textValue(payload.media_kind)} attachment` : "Analyzing attached media", tone: "neutral", source: "runtime" });
      return;
    }
    if (eventType === "audio_output.started") {
      items.push({ id: `runtime-${index}`, title: "Voice reply", detail: "Synthesizing a spoken reply", tone: "neutral", source: "runtime" });
      return;
    }
    if (eventType.startsWith("artifact.")) {
      const failed = eventType.includes("failed");
      items.push({ id: `runtime-${index}`, title: failed ? "Artifact failed" : "Artifact activity", detail: eventType.replaceAll(".", " "), tone: failed ? "danger" : "success", source: "runtime" });
      return;
    }
    if (eventType === "model.call.completed") {
      const total = typeof payload.total_tokens === "number" ? payload.total_tokens : 0;
      items.push({ id: `runtime-${index}`, title: "Model call completed", detail: total > 0 ? `${total.toLocaleString()} tokens processed` : "Model response completed", tone: "success", source: "runtime" });
      return;
    }
    if (eventType.startsWith("approval.")) {
      items.push({ id: `runtime-${index}`, title: "Approval activity", detail: eventType.replaceAll(".", " "), tone: eventType.includes("denied") || eventType.includes("unavailable") ? "danger" : "warning", source: "runtime" });
      return;
    }
    if (eventType === "checkpoint.saved") {
      const iteration = payload.iteration;
      items.push({ id: `runtime-${index}`, title: "Progress saved", detail: typeof iteration === "number" ? `Iteration ${iteration}` : "Checkpoint saved", tone: "success", source: "runtime" });
      return;
    }
    if (eventType === "verification.completed") {
      const summary = textValue(payload.summary);
      items.push({ id: `runtime-${index}`, title: "Verification completed", detail: summary || textValue(payload.status) || "Result checked", tone: payload.status === "failed" ? "danger" : "success", source: "runtime" });
      return;
    }
    if (eventType === "budget.exhausted") {
      items.push({ id: `runtime-${index}`, title: "Execution limit reached", detail: "The run reached its configured budget", tone: "warning", source: "runtime" });
    }
  });

  return items.slice(-40);
}

export function getModelIdentity(runtimeEvents: RuntimeActivity[]): { name: string; detail: string } {
  for (const event of [...runtimeEvents].reverse()) {
    if (event.event_type !== "model.call.completed") continue;
    const model = textValue(event.payload.model) || textValue(event.payload.model_name);
    const provider = textValue(event.payload.provider) || textValue(event.payload.provider_name);
    if (model && provider) return { name: model, detail: `via ${provider}` };
    if (model) return { name: model, detail: "Model reported by runtime" };
    if (provider) return { name: provider, detail: "Provider reported by runtime" };
  }
  return { name: "Automatic routing", detail: "EURUS selects from configured providers" };
}

export function getTokenUsage(events: AgentEventPayload[], runtimeEvents: RuntimeActivity[]): { input: number; output: number } {
  const agentUsage = events.reduce(
    (total, event) => ({
      input: total.input + (event.usage?.input_tokens ?? 0),
      output: total.output + (event.usage?.output_tokens ?? 0),
    }),
    { input: 0, output: 0 },
  );
  if (agentUsage.input + agentUsage.output > 0) return agentUsage;
  return runtimeEvents.reduce(
    (total, event) => event.event_type === "model.call.completed"
      ? {
          input: total.input + (typeof event.payload.input_tokens === "number" ? event.payload.input_tokens : 0),
          output: total.output + (typeof event.payload.output_tokens === "number" ? event.payload.output_tokens : 0),
        }
      : total,
    { input: 0, output: 0 },
  );
}

