/** MessageRenderer — typed message content rendering.
 *
 *  Message content is a plain string on the wire (the canonical EURUS
 *  contract), but it may embed structured blocks:
 *    - `[attach …]` markers → media blocks (image/audio/video/document)
 *    - `--- begin <file> --- … --- end <file> ---` → inlined document excerpts
 *  Text renders as plain text for user messages and markdown for assistant
 *  messages. Tool messages render as tool execution cards.
 */

import { memo, useState } from "react";
import { CheckCircle2, AlertTriangle, Terminal } from "lucide-react";
import type { AttachmentRef, MessageView } from "../../lib/types";
import { parseMessageContent } from "../../lib/media";
import { Markdown } from "../markdown/Markdown";
import { MediaBlock, ImageViewer } from "../media/MediaBlocks";
import { ToolChip } from "../EventTrace";

const EXCERPT_RE = /^---\s+begin\s+.+?\s+---$|^---\s+end\s+.+?\s+---$/m;

/** Render one text segment: markdown for assistant, preserved text for user. */
function TextSegment({ text, role }: { text: string; role: string }) {
  const trimmed = text.replace(/^\n+/, "").replace(/\n{2,}/g, "\n").trimEnd();
  if (!trimmed.trim()) return null;
  if (role === "assistant") {
    return <Markdown text={trimmed} />;
  }
  if (role === "user") {
    // Hide the backend's attachment annotation lines; media renders as blocks.
    const lines = trimmed.split("\n");
    const visible: string[] = [];
    let inExcerpt = false;
    for (const line of lines) {
      if (EXCERPT_RE.test(line.trim())) {
        inExcerpt = !inExcerpt && line.includes("begin");
        continue;
      }
      if (inExcerpt) continue;
      if (line.startsWith("Attachment:") || line.startsWith("(stored at")) continue;
      visible.push(line);
    }
    const text = visible.join("\n").trim();
    if (!text) return null;
    return (
      <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{text}</div>
    );
  }
  return (
    <div className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-dim">
      {trimmed}
    </div>
  );
}

export function MessageContent({ message }: { message: MessageView }) {
  const [viewerAttachment, setViewerAttachment] = useState<AttachmentRef | null>(null);
  const segments = parseMessageContent(message.content);

  return (
    <div className="min-w-0 flex-1 space-y-2">
      {segments.map((segment, index) =>
        segment.type === "text" && segment.text ? (
          <TextSegment key={index} text={segment.text} role={message.role} />
        ) : segment.attachment ? (
          <MediaBlock key={index} attachment={segment.attachment} onOpenImage={setViewerAttachment} />
        ) : null,
      )}
      {viewerAttachment && (
        <ImageViewer attachment={viewerAttachment} onClose={() => setViewerAttachment(null)} />
      )}
    </div>
  );
}

/** Durable tool message (persisted in the session history). */
export function ToolMessageCard({ message }: { message: MessageView }) {
  return (
    <ToolChip
      event={{
        kind: "tool_result",
        step: 0,
        tool_result: {
          call_id: message.tool_call_id ?? "",
          name: message.name ?? "tool",
          output: message.content,
          is_error: false,
          error_code: "",
        },
      }}
    />
  );
}

export const MessageRenderer = memo(function MessageRenderer({
  message,
}: {
  message: MessageView;
}) {
  if (message.role === "tool") {
    return <ToolMessageCard message={message} />;
  }
  return <MessageContent message={message} />;
});

/** Small helper re-exported for tool cards in activity lists. */
export function ToolStatusIcon({ isError }: { isError: boolean }) {
  return isError ? (
    <AlertTriangle size={11} className="text-danger" />
  ) : (
    <CheckCircle2 size={11} className="text-ok" />
  );
}

export function TerminalIcon() {
  return <Terminal size={12} className="text-copper" />;
}
