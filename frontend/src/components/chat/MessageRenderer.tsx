import { Component, memo, useMemo, useState, type ReactNode } from "react";
import { CheckCircle2, AlertTriangle, Terminal } from "lucide-react";
import type { AttachmentRef, ContentBlockView, MessageView, VideoBlockData, VideoFrameBlockData } from "../../lib/types";
import { parseMessageContent } from "../../lib/media";
import { Markdown } from "../markdown/Markdown";
import { ArtifactBlockCard, BlockAudio, BlockVideo, GeneratedMedia, MediaBlock, ImageViewer } from "../media/MediaBlocks";
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
      if (line.startsWith("Duration:") || line.startsWith("Resolution:")) continue;
      if (line.startsWith("Keyframes sampled at:")) continue;
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

interface GroupedBlock {
  kind: "text" | "image" | "audio" | "video" | "document" | "artifact";
  text?: string;
  image?: { asset_id: string; mime_type: string; alt_text?: string };
  audio?: { asset_id: string; mime_type: string; duration_seconds?: number; transcript?: string };
  video?: VideoBlockData;
  frames?: VideoFrameBlockData[];
  document?: { asset_id: string; mime_type: string; filename?: string; page_count?: number };
  artifact?: { artifact_id: string; artifact_type?: string; name?: string };
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

export function groupContentBlocks(blocks: ContentBlockView[]): GroupedBlock[] {
  const grouped: GroupedBlock[] = [];
  let videoIndexById = new Map<string, number>();
  videoIndexById = new Map();
  for (const block of blocks) {
    const data = block.data ?? {};
    switch (block.type) {
      case "text": {
        const text = typeof data.text === "string" ? data.text : "";
        if (text.trim()) grouped.push({ kind: "text", text });
        break;
      }
      case "image": {
        const assetId = asString(data.asset_id);
        if (!assetId) break;
        grouped.push({
          kind: "image",
          image: {
            asset_id: assetId,
            mime_type: asString(data.mime_type) ?? "image/png",
            alt_text: asString(data.alt_text),
          },
        });
        break;
      }
      case "audio": {
        const assetId = asString(data.asset_id);
        if (!assetId) break;
        grouped.push({
          kind: "audio",
          audio: {
            asset_id: assetId,
            mime_type: asString(data.mime_type) ?? "audio/mpeg",
            duration_seconds: asNumber(data.duration_seconds),
            transcript: asString(data.transcript),
          },
        });
        break;
      }
      case "video": {
        const assetId = asString(data.asset_id);
        if (!assetId) break;
        grouped.push({
          kind: "video",
          video: {
            type: "video",
            asset_id: assetId,
            mime_type: asString(data.mime_type) ?? "video/mp4",
            duration_seconds: asNumber(data.duration_seconds),
            thumbnail_asset_id: asString(data.thumbnail_asset_id),
            transcript: asString(data.transcript),
          },
          frames: [],
        });
        videoIndexById.set(assetId, grouped.length - 1);
        break;
      }
      case "video_frame": {
        const assetId = asString(data.asset_id);
        const sourceId = asString(data.source_asset_id);
        const stamp = asNumber(data.timestamp_seconds);
        if (!assetId || !sourceId || stamp === undefined) break;
        const target = videoIndexById.has(sourceId) ? grouped[videoIndexById.get(sourceId) as number] : undefined;
        const frame: VideoFrameBlockData = {
          type: "video_frame",
          asset_id: assetId,
          source_asset_id: sourceId,
          mime_type: asString(data.mime_type) ?? "image/jpeg",
          timestamp_seconds: stamp,
        };
        if (target && target.kind === "video") {
          target.frames = [...(target.frames ?? []), frame];
        } else {
          grouped.push({ kind: "image", image: { asset_id: assetId, mime_type: frame.mime_type, alt_text: `keyframe at ${stamp}s` } });
        }
        break;
      }
      case "document": {
        const assetId = asString(data.asset_id);
        if (!assetId) break;
        grouped.push({
          kind: "document",
          document: {
            asset_id: assetId,
            mime_type: asString(data.mime_type) ?? "application/octet-stream",
            filename: asString(data.filename),
            page_count: asNumber(data.page_count),
          },
        });
        break;
      }
      case "artifact": {
        const artifactId = asString(data.artifact_id);
        if (!artifactId) break;
        grouped.push({
          kind: "artifact",
          artifact: {
            artifact_id: artifactId,
            artifact_type: asString(data.artifact_type),
            name: asString(data.name),
          },
        });
        break;
      }
      default:
        break;
    }
  }
  return grouped;
}

const BlockImage = memo(function BlockImage({
  assetId,
  mime,
  altText,
  onOpen,
}: {
  assetId: string;
  mime: string;
  altText?: string;
  onOpen: (attachment: AttachmentRef) => void;
}) {
  const attachment: AttachmentRef = {
    id: assetId,
    kind: "image",
    name: altText || "image",
    mime,
    size: 0,
  };
  return <MediaBlock attachment={attachment} onOpenImage={onOpen} />;
});

const BlockDocument = memo(function BlockDocument({
  assetId,
  mime,
  filename,
}: {
  assetId: string;
  mime: string;
  filename?: string;
}) {
  const attachment: AttachmentRef = {
    id: assetId,
    kind: "document",
    name: filename || "document",
    mime,
    size: 0,
  };
  return <MediaBlock attachment={attachment} onOpenImage={() => undefined} />;
});

const BlockList = memo(function BlockList({
  blocks,
  role,
}: {
  blocks: ContentBlockView[];
  role: string;
}) {
  const [viewerAttachment, setViewerAttachment] = useState<AttachmentRef | null>(null);
  const grouped = useMemo(() => groupContentBlocks(blocks), [blocks]);
  return (
    <div className="min-w-0 flex-1 space-y-2">
      {grouped.map((item, index) => {
        if (item.kind === "text") {
          return <TextSegment key={index} text={item.text ?? ""} role={role} />;
        }
        if (item.kind === "image" && item.image) {
          return (
            <BlockImage
              key={index}
              assetId={item.image.asset_id}
              mime={item.image.mime_type}
              altText={item.image.alt_text}
              onOpen={setViewerAttachment}
            />
          );
        }
        if (item.kind === "audio" && item.audio) {
          return (
            <BlockAudio
              key={index}
              assetId={item.audio.asset_id}
              mime={item.audio.mime_type}
              durationSeconds={item.audio.duration_seconds}
              transcript={item.audio.transcript}
            />
          );
        }
        if (item.kind === "video" && item.video) {
          return <BlockVideo key={index} block={item.video} frames={item.frames ?? []} />;
        }
        if (item.kind === "document" && item.document) {
          return (
            <BlockDocument
              key={index}
              assetId={item.document.asset_id}
              mime={item.document.mime_type}
              filename={item.document.filename}
            />
          );
        }
        if (item.kind === "artifact" && item.artifact) {
          return (
            <ArtifactBlockCard
              key={index}
              artifactId={item.artifact.artifact_id}
              fallbackType={item.artifact.artifact_type}
              fallbackName={item.artifact.name}
            />
          );
        }
        return null;
      })}
      {viewerAttachment && (
        <ImageViewer attachment={viewerAttachment} onClose={() => setViewerAttachment(null)} />
      )}
    </div>
  );
});

const MessageContent = memo(function MessageContent({ message }: { message: MessageView }) {
  const [viewerAttachment, setViewerAttachment] = useState<AttachmentRef | null>(null);
  const segments = useMemo(() => parseMessageContent(message.content), [message.content]);

  if (message.blocks && message.blocks.length > 0) {
    return <BlockList blocks={message.blocks} role={message.role} />;
  }

  return (
    <div className="min-w-0 flex-1 space-y-2">
      {segments.map((segment, index) =>
        segment.type === "text" && segment.text ? (
          <TextSegment key={index} text={segment.text} role={message.role} />
        ) : segment.attachment ? (
          <MediaBlock key={index} attachment={segment.attachment} onOpenImage={setViewerAttachment} />
        ) : segment.generated ? (
          <GeneratedMedia key={index} id={segment.generated.id} kind={segment.generated.kind} />
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
