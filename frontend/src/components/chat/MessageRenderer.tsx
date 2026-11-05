import { Component, memo, useMemo, useState, type ReactNode } from "react";
import { CheckCircle2, AlertTriangle, Terminal } from "lucide-react";
import type { AttachmentRef, MessageView } from "../../lib/types";
import { parseMessageContent } from "../../lib/media";
import { Markdown } from "../markdown/Markdown";
import { MediaBlock, ImageViewer } from "../media/MediaBlocks";
import { ToolChip } from "../EventTrace";

const EXCERPT_RE = /^---\s+begin\s+.+?\s+---$|^---\s+end\s+.+?\s+---$/m;

interface MessageErrorBoundaryProps {
  children: ReactNode;
}

interface MessageErrorBoundaryState {
  failed: boolean;
}

class MessageErrorBoundary extends Component<MessageErrorBoundaryProps, MessageErrorBoundaryState> {
  state: MessageErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): MessageErrorBoundaryState {
    return { failed: true };
  }


  render() {
    if (this.state.failed) {
      return (
        <div role="alert" className="rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger">
          This message could not be rendered. The rest of the conversation is still available.
        </div>
      );
    }
    return this.props.children;
  }
}

const TextSegment = memo(function TextSegment({ text, role }: { text: string; role: string }) {
  if (role === "assistant") {
    return text.trim() ? <Markdown text={text} /> : null;
  }
  const trimmed = text.replace(/^\n+/, "").trimEnd();
  if (!trimmed.trim()) return null;
  if (role === "user") {
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
    const visibleText = visible.join("\n").trim();
    if (!visibleText) return null;
    return (
      <div className="whitespace-pre-wrap break-words text-sm leading-relaxed text-ink">{visibleText}</div>
    );
  }
  return (
    <div className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-dim">
      {trimmed}
    </div>
  );
});

const MessageContent = memo(function MessageContent({ message }: { message: MessageView }) {
  const [viewerAttachment, setViewerAttachment] = useState<AttachmentRef | null>(null);
  const segments = useMemo(() => parseMessageContent(message.content), [message.content]);

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
});

function ToolMessageCard({ message }: { message: MessageView }) {
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

export const MessageRenderer = memo(function MessageRenderer({ message }: { message: MessageView }) {
  return (
    <MessageErrorBoundary>
      {message.role === "tool" ? <ToolMessageCard message={message} /> : <MessageContent message={message} />}
    </MessageErrorBoundary>
  );
});

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
