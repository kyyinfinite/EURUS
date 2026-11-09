import { describe, expect, it } from "vitest";
import type { AgentEventPayload } from "./types";
import {
  buildActivityTimeline,
  describeCurrentActivity,
  getModelIdentity,
  getTokenUsage,
} from "./thinking";

const toolCall: AgentEventPayload = {
  kind: "tool_call",
  step: 2,
  tool_call: { id: "call-1", name: "web_search", arguments: { query: "EURUS" } },
};

const toolResult: AgentEventPayload = {
  kind: "tool_result",
  step: 2,
  tool_result: {
    call_id: "call-1",
    name: "web_search",
    output: "Found results",
    is_error: false,
    error_code: "",
  },
};

describe("thinking experience data", () => {
  it("describes the latest meaningful agent activity", () => {
    expect(describeCurrentActivity([])).toBe("Preparing your request");
    expect(describeCurrentActivity([toolCall])).toBe("Using web_search");
    expect(describeCurrentActivity([toolCall, toolResult])).toBe("Finished web_search");
  });

  it("builds a safe timeline without exposing tool arguments or outputs", () => {
    const timeline = buildActivityTimeline(
      [toolCall, toolResult],
      [{ event_type: "checkpoint.saved", payload: { iteration: 3 }, created_at: "2026-11-01T00:00:00Z" }],
    );
    expect(timeline.map((item) => item.title)).toEqual([
      "Tool started",
      "Tool completed",
      "Progress saved",
    ]);
    expect(JSON.stringify(timeline)).not.toContain("EURUS");
    expect(JSON.stringify(timeline)).not.toContain("Found results");
  });

  it("uses an honest automatic route when model identity is not reported", () => {
    expect(getModelIdentity([])).toEqual({
      name: "Automatic routing",
      detail: "EURUS selects from configured providers",
    });
  });

  it("uses runtime model identity only when explicitly reported", () => {
    expect(getModelIdentity([
      {
        event_type: "model.call.completed",
        payload: { model: "qwen-3", provider: "openrouter" },
        created_at: "2026-11-01T00:00:00Z",
      },
    ])).toEqual({ name: "qwen-3", detail: "via openrouter" });
  });

  it("aggregates token usage from agent or runtime events", () => {
    const event: AgentEventPayload = {
      kind: "model_call",
      step: 1,
      usage: {
        input_tokens: 120,
        output_tokens: 45,
        cache_creation_tokens: 0,
        cache_read_tokens: 0,
      },
    };
    expect(getTokenUsage([event], [])).toEqual({ input: 120, output: 45 });
    expect(getTokenUsage([], [{
      event_type: "model.call.completed",
      payload: { input_tokens: 60, output_tokens: 20 },
      created_at: "2026-11-01T00:00:00Z",
    }])).toEqual({ input: 60, output: 20 });
  });
});

describe("multimodal activity descriptions", () => {
  it("describes media analysis and generation activity", () => {
    expect(
      describeCurrentActivity([{ kind: "media_input_analyzing", step: 1, media_kind: "image" }]),
    ).toBe("Analyzing image input");
    expect(
      describeCurrentActivity([
        { kind: "content_block_started", step: 1, block: { type: "image" } },
      ]),
    ).toBe("Composing a image response block");
    expect(
      describeCurrentActivity([{ kind: "audio_output_started", step: 1 }]),
    ).toBe("Preparing a spoken reply");
    expect(
      describeCurrentActivity([{ kind: "artifact", step: 1, artifact_id: "x", artifact_status: "failed" }]),
    ).toBe("Artifact generation failed");
  });

  it("builds timeline entries for multimodal agent events", () => {
    const timeline = buildActivityTimeline(
      [
        { kind: "media_input_analyzing", step: 1, media_kind: "video" },
        { kind: "artifact", step: 2, artifact_id: "abcd1234", artifact_status: "ready" },
      ],
      [],
    );
    expect(timeline.map((item) => item.title)).toEqual(["Media analysis", "Artifact ready"]);
    expect(timeline[0]?.detail).toContain("video");
  });

  it("builds timeline entries for generation runtime events", () => {
    const timeline = buildActivityTimeline(
      [],
      [
        { event_type: "media.generation.started", payload: { kind: "video" }, created_at: "" },
        { event_type: "media.generation.ready", payload: { kind: "video", artifact_id: "a1" }, created_at: "" },
        { event_type: "media.generation.failed", payload: { error: "quota" }, created_at: "" },
        { event_type: "audio_output.started", payload: {}, created_at: "" },
      ],
    );
    expect(timeline.map((item) => item.title)).toEqual([
      "Media generation",
      "Media generated",
      "Media generation failed",
      "Voice reply",
    ]);
    expect(timeline[2]?.tone).toBe("danger");
  });
});
