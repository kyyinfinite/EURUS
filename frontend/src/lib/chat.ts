import type { ContentBlockView, MediaView, MessageView } from "./types";

export function createOptimisticUserMessage(
  content: string,
  previousMessages: MessageView[] | undefined,
  attachments: MediaView[] = [],
): MessageView {
  const sequence = (previousMessages ?? []).reduce(
    (maximum, message) => Math.max(maximum, message.sequence),
    0,
  ) + 1;
  const blocks: ContentBlockView[] = attachments.map((media) => ({
    type: media.kind === "document" ? "document" : media.kind,
    data: {
      type: media.kind === "document" ? "document" : media.kind,
      asset_id: media.id,
      mime_type: media.mime,
      filename: media.filename,
    },
  }));
  return {
    sequence,
    role: "user",
    content,
    tool_calls: [],
    tool_call_id: null,
    name: null,
    blocks,
  };
}
