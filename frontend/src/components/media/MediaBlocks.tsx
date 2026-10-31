/** Media rendering blocks: image viewer, audio player, document preview,
 *  video player and the composer attachment preview.
 *
 *  All payloads are fetched through the authenticated API (object URLs,
 *  cached per media id). This is the ONE media rendering implementation —
 *  used by the intelligence renderer, the chat composer and the Files page.
 */

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Download,
  FileText,
  FileJson,
  FileCode2,
  FileSpreadsheet,
  FileType2,
  ImageIcon,
  Loader2,
  Music,
  Pause,
  Play,
  RotateCcw,
  RotateCw,
  Video,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { AttachmentRef, MediaKind, MediaView } from "../../lib/types";
import { useMediaUrl } from "../../lib/media";
import { formatBytes } from "../../lib/format";
import { Badge, IconButton } from "../ui";

/** Icon for a document by MIME / extension (used across previews + Files). */
export function DocumentIcon({ mime, name, size = 16 }: { mime: string; name: string; size?: number }) {
  const ext = name.split(".").pop()?.toLowerCase() ?? "";
  if (["json", "xml"].includes(ext) || mime.includes("json") || mime.includes("xml")) {
    return <FileJson size={size} />;
  }
  if (["py", "js", "ts", "tsx", "jsx", "html", "css", "sql", "sh", "yaml", "yml", "toml", "go", "rs", "c", "cpp", "java", "rst"].includes(ext)) {
    return <FileCode2 size={size} />;
  }
  if (["xlsx", "csv"].includes(ext) || mime.includes("spreadsheet") || mime.includes("csv")) {
    return <FileSpreadsheet size={size} />;
  }
  if (mime === "application/pdf" || ext === "pdf") {
    return <FileType2 size={size} />;
  }
  return <FileText size={size} />;
}

/* ---- image viewer (lightbox) -------------------------------------------------- */

export function ImageViewer({
  attachment,
  onClose,
}: {
  attachment: AttachmentRef;
  onClose: () => void;
}) {
  const { url, error } = useMediaUrl(attachment);
  const [zoom, setZoom] = useState(1);
  const [rotation, setRotation] = useState(0);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key === "+" || event.key === "=") setZoom((z) => Math.min(4, z + 0.25));
      if (event.key === "-") setZoom((z) => Math.max(0.5, z - 0.25));
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[85] flex animate-fade-in flex-col bg-ink/92">
      <header className="flex items-center justify-between gap-3 border-b border-line-soft bg-surface px-4 py-2.5 safe-top">
        <div className="flex min-w-0 items-center gap-2">
          <ImageIcon size={15} className="shrink-0 text-copper" />
          <span className="truncate text-sm text-ink">{attachment.name}</span>
          <Badge tone="dim" className="hidden sm:inline-flex">
            {formatBytes(attachment.size)}
          </Badge>
        </div>
        <div className="flex items-center gap-1">
          <IconButton label="Zoom out" onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))} size={34}>
            <ZoomOut size={15} className="text-muted" />
          </IconButton>
          <span className="w-11 text-center font-mono text-xs text-muted">{Math.round(zoom * 100)}%</span>
          <IconButton label="Zoom in" onClick={() => setZoom((z) => Math.min(4, z + 0.25))} size={34}>
            <ZoomIn size={15} className="text-muted" />
          </IconButton>
          <IconButton
            label="Rotate left"
            onClick={() => setRotation((r) => r - 90)}
            size={34}
            className="hidden sm:inline-flex"
          >
            <RotateCcw size={15} className="text-muted" />
          </IconButton>
          <IconButton label="Rotate right" onClick={() => setRotation((r) => r + 90)} size={34}>
            <RotateCw size={15} className="text-muted" />
          </IconButton>
          <a
            href={url ?? undefined}
            download={attachment.name}
            aria-label="Download image"
            title="Download image"
            className="inline-flex h-[34px] w-[34px] items-center justify-center rounded-lg border border-transparent text-muted transition-colors hover:bg-raised-soft hover:text-ink"
          >
            <Download size={15} />
          </a>
          <IconButton label="Close viewer" onClick={onClose} size={34}>
            <X size={15} className="text-muted" />
          </IconButton>
        </div>
      </header>
      <div
        className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-ink/92 p-4"
        onClick={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
      >
        {error ? (
          <div className="flex flex-col items-center gap-2 text-sm text-danger">
            <AlertTriangle size={20} />
            {error}
          </div>
        ) : !url ? (
          <Loader2 size={22} className="animate-spin text-copper" />
        ) : (
          <img
            src={url}
            alt={attachment.name}
            className="max-h-full max-w-full select-none rounded-lg shadow-overlay transition-transform duration-150"
            style={{ transform: `scale(${zoom}) rotate(${rotation}deg)` }}
          />
        )}
      </div>
    </div>
  );
}

/* ---- audio player ---------------------------------------------------------------- */

export function AudioPlayer({
  attachment,
  compact = false,
}: {
  attachment: AttachmentRef;
  compact?: boolean;
}) {
  const { url, error } = useMediaUrl(attachment);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const onTime = () => setPosition(audio.currentTime);
    const onMeta = () => setDuration(audio.duration || 0);
    const onEnd = () => setPlaying(false);
    audio.addEventListener("timeupdate", onTime);
    audio.addEventListener("loadedmetadata", onMeta);
    audio.addEventListener("ended", onEnd);
    return () => {
      audio.removeEventListener("timeupdate", onTime);
      audio.removeEventListener("loadedmetadata", onMeta);
      audio.removeEventListener("ended", onEnd);
    };
  }, [url]);

  const toggle = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
      setPlaying(false);
    } else {
      void audio
        .play()
        .then(() => setPlaying(true))
        .catch(() => setPlaying(false));
    }
  };

  const fmt = (seconds: number) =>
    `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

  return (
    <div
      className={`flex items-center gap-3 rounded-xl border border-line bg-surface ${
        compact ? "px-3 py-2" : "px-3.5 py-3"
      }`}
    >
      <button
        type="button"
        onClick={toggle}
        disabled={!url || Boolean(error)}
        aria-label={playing ? "Pause audio" : "Play audio"}
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full border border-copper/40 bg-copper-soft text-copper-dark transition-colors hover:bg-copper/20 disabled:opacity-40"
      >
        {playing ? <Pause size={16} /> : <Play size={16} className="ml-0.5" />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-[13px] text-dim">{attachment.name}</span>
          <span className="shrink-0 font-mono text-2xs text-faint">
            {fmt(position)} / {duration ? fmt(duration) : "–:––"}
          </span>
        </div>
        <input
          type="range"
          min={0}
          max={duration || 0}
          step={0.1}
          value={position}
          onChange={(event) => {
            const audio = audioRef.current;
            const next = Number(event.target.value);
            setPosition(next);
            if (audio) audio.currentTime = next;
          }}
          aria-label="Seek audio"
          className="mt-1.5 h-1.5 w-full cursor-pointer appearance-none rounded-full !border-0 !bg-line !p-0 accent-copper"
        />
      </div>
      {url && <audio ref={audioRef} src={url} preload="metadata" className="hidden" />}
      {error && <span className="text-xs text-danger">{error}</span>}
    </div>
  );
}

/* ---- document preview -------------------------------------------------------------- */

export function DocumentPreview({ attachment }: { attachment: AttachmentRef }) {
  const { url, error } = useMediaUrl(attachment);
  return (
    <div className="flex items-center gap-3 rounded-xl border border-line bg-surface px-3.5 py-3">
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-line bg-raised-soft text-muted">
        <DocumentIcon mime={attachment.mime} name={attachment.name} size={17} />
      </span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm text-ink">{attachment.name}</p>
        <p className="mt-0.5 font-mono text-2xs text-faint">
          {attachment.mime} · {formatBytes(attachment.size)}
        </p>
      </div>
      {error ? (
        <span className="shrink-0 text-xs text-danger">load failed</span>
      ) : url ? (
        <a
          href={url}
          download={attachment.name}
          className="shrink-0 rounded-lg border border-line px-2.5 py-1.5 text-xs font-medium text-dim transition-colors hover:border-copper/50 hover:text-copper-dark"
        >
          Open
        </a>
      ) : (
        <Loader2 size={14} className="shrink-0 animate-spin text-faint" />
      )}
    </div>
  );
}

/* ---- generic media block used by the intelligence renderer ------------------------- */

export function MediaBlock({
  attachment,
  onOpenImage,
}: {
  attachment: AttachmentRef;
  onOpenImage: (attachment: AttachmentRef) => void;
}) {
  if (attachment.kind === "image") {
    return <ImageThumb attachment={attachment} onOpen={onOpenImage} />;
  }
  if (attachment.kind === "audio") {
    return <AudioPlayer attachment={attachment} />;
  }
  if (attachment.kind === "video") {
    return <VideoBlock attachment={attachment} />;
  }
  return <DocumentPreview attachment={attachment} />;
}

function ImageThumb({
  attachment,
  onOpen,
}: {
  attachment: AttachmentRef;
  onOpen: (attachment: AttachmentRef) => void;
}) {
  const { url, error } = useMediaUrl(attachment);
  return (
    <button
      type="button"
      onClick={() => onOpen(attachment)}
      className="group relative block overflow-hidden rounded-xl border border-line bg-surface transition-all hover:border-copper/50 hover:shadow-float"
      aria-label={`View image ${attachment.name}`}
    >
      {error ? (
        <span className="flex h-28 w-44 flex-col items-center justify-center gap-1.5 text-xs text-danger">
          <AlertTriangle size={15} /> failed to load
        </span>
      ) : !url ? (
        <span className="flex h-28 w-44 items-center justify-center">
          <Loader2 size={17} className="animate-spin text-faint" />
        </span>
      ) : (
        <img
          src={url}
          alt={attachment.name}
          loading="lazy"
          className="block max-h-60 w-auto object-cover"
        />
      )}
      <span className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-ink/80 px-2.5 py-1.5 opacity-0 transition-opacity group-hover:opacity-100">
        <span className="truncate text-left font-mono text-2xs text-canvas">{attachment.name}</span>
        <span className="shrink-0 font-mono text-2xs text-canvas/70">{formatBytes(attachment.size)}</span>
      </span>
    </button>
  );
}

function VideoBlock({ attachment }: { attachment: AttachmentRef }) {
  const { url, error } = useMediaUrl(attachment);
  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-danger/30 bg-danger-soft px-3.5 py-2.5 text-xs text-danger">
        <Video size={14} /> {attachment.name} — {error}
      </div>
    );
  }
  if (!url) {
    return (
      <div className="flex h-32 w-full max-w-md items-center justify-center rounded-xl border border-line bg-surface">
        <Loader2 size={17} className="animate-spin text-faint" />
      </div>
    );
  }
  return (
    <video
      src={url}
      controls
      preload="metadata"
      className="max-h-80 w-full max-w-md rounded-xl border border-line bg-ink"
      aria-label={attachment.name}
    />
  );
}

/* ---- composer attachment preview (chips) ---------------------------------------------- */

export interface PendingAttachment {
  localId: string;
  file: File;
  media?: MediaView;
  progress: number;
  error: string | null;
  cancel?: () => void;
}

export function AttachmentPreview({
  item,
  onRemove,
  onRetry,
}: {
  item: PendingAttachment;
  onRemove: () => void;
  onRetry: () => void;
}) {
  const [thumbUrl, setThumbUrl] = useState<string | null>(null);

  useEffect(() => {
    if (item.file.type.startsWith("image/")) {
      const url = URL.createObjectURL(item.file);
      setThumbUrl(url);
      return () => URL.revokeObjectURL(url);
    }
    return undefined;
  }, [item.file]);

  const uploading = !item.media && !item.error;
  const failed = Boolean(item.error);
  const kind: MediaKind | null = item.media?.kind ?? null;

  return (
    <div
      className={`relative flex w-28 shrink-0 flex-col overflow-hidden rounded-xl border bg-surface ${
        failed ? "border-danger/50" : "border-line"
      }`}
    >
      <div className="relative flex h-16 items-center justify-center bg-raised-soft">
        {thumbUrl ? (
          <img src={thumbUrl} alt="" className="h-full w-full object-cover" />
        ) : kind === "audio" || item.file.type.startsWith("audio/") ? (
          <Music size={16} className="text-copper" />
        ) : kind === "video" || item.file.type.startsWith("video/") ? (
          <Video size={16} className="text-copper" />
        ) : (
          <span className="text-copper">
            <DocumentIcon mime={item.file.type} name={item.file.name} size={16} />
          </span>
        )}
        {uploading && (
          <div
            className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-surface/80"
            role="progressbar"
            aria-valuenow={Math.round(item.progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <span className="h-1 w-16 overflow-hidden rounded-full bg-line">
              <span
                className="block h-full rounded-full bg-copper transition-[width]"
                style={{ width: `${Math.max(6, item.progress * 100)}%` }}
              />
            </span>
            <span className="font-mono text-2xs text-copper-dark">
              {Math.round(item.progress * 100)}%
            </span>
          </div>
        )}
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${item.file.name}`}
          className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-ink/70 text-canvas transition-colors hover:bg-danger hover:text-white"
        >
          <X size={11} />
        </button>
      </div>
      <div className="flex items-center gap-1 px-2 py-1.5">
        <span className="truncate text-2xs text-dim" title={item.file.name}>
          {item.file.name}
        </span>
        <span className="shrink-0 font-mono text-2xs text-faint">{formatBytes(item.file.size)}</span>
      </div>
      {failed && (
        <div>
          <button
            type="button"
            onClick={onRetry}
            className="w-full border-t border-danger/25 px-2 py-1 text-left text-2xs font-medium text-danger transition-colors hover:bg-danger-soft"
          >
            {item.error && item.error.length > 26 ? "retry upload" : `${item.error} · retry`}
          </button>
        </div>
      )}
    </div>
  );
}
