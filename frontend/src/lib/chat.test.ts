import { describe, expect, it } from "vitest";
import { createOptimisticUserMessage } from "./chat";

const message = {
  sequence: 8,
  role: "assistant" as const,
  content: "answer",
  tool_calls: [],
  tool_call_id: null,
  name: null,
};

describe("createOptimisticUserMessage", () => {
  it("creates a user message after the current sequence", () => {
    expect(createOptimisticUserMessage("hello", [message])).toEqual({
      sequence: 9,
      role: "user",
      content: "hello",
      tool_calls: [],
      tool_call_id: null,
      name: null,
      blocks: [],
    });
  });

  it("starts at sequence one when the conversation has no messages", () => {
    expect(createOptimisticUserMessage("first", undefined).sequence).toBe(1);
  });
});

describe("createOptimisticUserMessage with attachments", () => {
  const attachments = [
    {
      id: "11111111111111111111111111111111",
      session_id: "s1",
      filename: "photo.png",
      mime: "image/png",
      kind: "image" as const,
      size: 2048,
      sha256: "x",
      created_at: "2026-11-01T00:00:00Z",
      url: "/api/media/1",
    },
    {
      id: "22222222222222222222222222222222",
      session_id: "s1",
      filename: "clip.mp4",
      mime: "video/mp4",
      kind: "video" as const,
      size: 8192,
      sha256: "y",
      created_at: "2026-11-01T00:00:00Z",
      url: "/api/media/2",
    },
  ];

  it("carries attachment content blocks so media renders immediately", () => {
    const optimistic = createOptimisticUserMessage("check this", [message], attachments);
    expect(optimistic.role).toBe("user");
    expect(optimistic.blocks).toHaveLength(2);
    expect(optimistic.blocks?.[0]).toEqual({
      type: "image",
      data: {
        type: "image",
        asset_id: "11111111111111111111111111111111",
        mime_type: "image/png",
        filename: "photo.png",
      },
    });
    expect(optimistic.blocks?.[1]?.type).toBe("video");
  });

  it("omits blocks when no attachments were uploaded", () => {
    const optimistic = createOptimisticUserMessage("hi", undefined);
    expect(optimistic.blocks).toEqual([]);
  });
});
