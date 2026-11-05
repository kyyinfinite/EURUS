import type { MessageView } from "./types";

export function createOptimisticUserMessage(
  content: string,
  previousMessages: MessageView[] | undefined,
): MessageView {
  const sequence = (previousMessages ?? []).reduce(
    (maximum, message) => Math.max(maximum, message.sequence),
    0,
  ) + 1;
  return {
    sequence,
    role: "user",
    content,
    tool_calls: [],
    tool_call_id: null,
    name: null,
  };
}
