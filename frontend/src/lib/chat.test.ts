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
    });
  });

  it("starts at sequence one when the conversation has no messages", () => {
    expect(createOptimisticUserMessage("first", undefined).sequence).toBe(1);
  });
});
