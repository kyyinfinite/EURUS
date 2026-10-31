/** Media rendering blocks: image viewer, audio player, document preview,
 *  and composer attachment chips. All payloads are fetched through the
 *  authenticated API (object URLs, cached per media id). */

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Download,
  FileText,
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
import type { AttachmentRef, MediaView } from "../../lib/types";
import { useMediaUrl } from "../../lib/media";
import { formatBytes } from "../../lib/format";
import { Badge, IconButton } from "../ui";

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
    <div className="fixed inset-0 z-[85] flex flex-col animate-fade-in bg-obsidian/95">
      <header className="flex items-center justify-between gap-3 border-b border-edge px-4 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <ImageIcon size={14} className="shrink-0 text-copper" />
          <span className="truncate text-xs text-ink">{attachment.name}</span>
          <Badge tone="dim" className="hidden sm:inline-flex">
            {formatBytes(attachment.size)}
          </Badge>
        </div>
        <div className="flex items-center gap-1">
          <IconButton label="Zoom out" onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))} size={32}>
            <ZoomOut size={15} className="text-dim" />
          </IconButton>
          <span className="w-10 text-center font-mono text-2xs text-faint">{Math.round(zoom * 100)}%</span>
          <IconButton label="Zoom in" onClick={() => setZoom((z) => Math.min(4, z + 0.25))} size={32}>
            <ZoomIn size={15} className="text-dim" />
          </IconButton>
          <IconButton
            label="Rotate left"
            onClick={() => setRotation((r) => r - 90)}
            size={32}
            className="hidden sm:inline-flex"
          >
            <RotateCcw size={15} className="text-dim" />
          </IconButton>
          <IconButton label="Rotate right" onClick={() => setRotation((r) => r + 90)} size={32}>
            <RotateCw size={15} className="text-dim" />
          </IconButton>
          <a
            href={url ?? undefined}
            download={attachment.name}
            aria-label="Download image"
            title="Download image"
            className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-transparent text-dim transition-colors hover:bg-raised hover:text-ink"
          >
            <Download size={15} />
          </a>
          <IconButton label="Close viewer" onClick={onClose} size={32}>
            <X size={15} className="text-dim" />
          </IconButton>
        </div>
      </header>
      <div
        className="flex min-h-0 flex-1 items-center justify-center overflow-auto p-4"
        onClick={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
      >
        {error ? (
          <div className="flex flex-col items-center gap-2 text-xs text-danger">
            <AlertTriangle size={18} />
            {error}
          </div>
        ) : !url ? (
          <Loader2 size={20} className="animate-spin text-copper" />
        ) : (
          <img
            src={url}
            alt={attachment.name}
            className="max-h-full max-w-full select-none rounded-sm shadow-overlay transition-transform duration-150"
            style={{ transform: `scale(${zoom}) rotate(${rotation}deg)` }}
          />
        )}
      </div>
    </div>
  );
}

/* ---- audio player ---------------------------------------------------------------- */

export function AudioPlayer({ attachment, compact = false }: { attachment: AttachmentRef; compact?: boolean }) {
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
      className={`flex items-center gap-3 rounded-md border border-edge bg-obsidian/60 ${
        compact ? "px-2.5 py-2" : "px-3 py-2.5"
      }`}
    >
      <button
        type="button"
        onClick={toggle}
        disabled={!url || Boolean(error)}
        aria-label={playing ? "Pause audio" : "Play audio"}
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-copper/50 bg-copper/10 text-copper transition-colors hover:bg-copper/20 disabled:opacity-40"
      >
        {playing ? <Pause size={15} /> : <Play size={15} className="ml-0.5" />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-2xs text-dim">{attachment.name}</span>
          <span className="shrink-0 font-mono text-3xs text-faint">
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
          className="mt-1 h-1 w-full cursor-pointer appearance-none rounded-full border-0 bg-edge p-0 accent-copper"
        />
      </div>
      {url && <audio ref={audioRef} src={url} preload="metadata" className="hidden" />}
      {error && <span className="text-2xs text-danger">{error}</span>}
    </div>
  );
}

/* ---- document preview -------------------------------------------------------------- */

export function DocumentPreview({ attachment }: { attachment: AttachmentRef }) {
  const { url, error } = useMediaUrl(attachment);
  const docIcon = (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-sage/40 bg-sage/10">
      <FileText size={15} className="text-sage" />
    </span>
  );
  return (
    <div className="flex items-center gap-3 rounded-md border border-edge bg-obsidian/60 px-3 py-2.5">
      {docIcon}
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs text-ink">{attachment.name}</p>
        <p className="mt-0.5 font-mono text-3xs text-faint">
          {attachment.mime} · {formatBytes(attachment.size)}
        </p>
      </div>
      {error ? (
        <span className="shrink-0 text-2xs text-danger">load failed</span>
      ) : url ? (
        <a
          href={url}
          download={attachment.name}
          className="shrink-0 rounded-sm border border-edge px-2 py-1 text-2xs text-dim transition-colors hover:border-copper/50 hover:text-copper"
        >
          Open
        </a>
      ) : (
        <Loader2 size={13} className="shrink-0 animate-spin text-faint" />
      )}
    </div>
  );
}

/* ---- generic media block used by the message renderer --------------------------------- */

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
      className="group relative block max-w-xs overflow-hidden rounded-md border border-edge transition-colors hover:border-copper/50"
      aria-label={`View image ${attachment.name}`}
    >
      {error ? (
        <span className="flex h-24 w-40 flex-col items-center justify-center gap-1.5 text-2xs text-danger">
          <AlertTriangle size={14} /> failed to load
        </span>
      ) : !url ? (
        <span className="flex h-24 w-40 items-center justify-center">
          <Loader2 size={16} className="animate-spin text-faint" />
        </span>
      ) : (
        <img
          src={url}
          alt={attachment.name}
          loading="lazy"
          className="block max-h-56 w-auto object-cover"
        />
      )}
      <span className="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-obsidian/85 px-2 py-1.5 opacity-0 transition-opacity group-hover:opacity-100">
        <span className="truncate text-left font-mono text-3xs text-dim">{attachment.name}</span>
        <span className="shrink-0 font-mono text-3xs text-faint">{formatBytes(attachment.size)}</span>
      </span>
    </button>
  );
}

function VideoBlock({ attachment }: { attachment: AttachmentRef }) {
  const { url, error } = useMediaUrl(attachment);
  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-2xs text-danger">
        <Video size={13} /> {attachment.name} — {error}
      </div>
    );
  }
  if (!url) {
    return (
      <div className="flex h-28 w-full max-w-md items-center justify-center rounded-md border border-edge">
        <Loader2 size={16} className="animate-spin text-faint" />
      </div>
    );
  }
  return (
    <video
      src={url}
      controls
      preload="metadata"
      className="max-h-72 w-full max-w-md rounded-md border border-edge"
      aria-label={attachment.name}
    />
  );
}

/* ---- composer attachment chip ----------------------------------------------------------- */

export interface PendingAttachment {
  localId: string;
  file: File;
  media?: MediaView;
  progress: number;
  error: string | null;
}

export function AttachmentChip({
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

  return (
    <div
      className={`relative flex w-24 shrink-0 flex-col overflow-hidden rounded-md border ${
        failed ? "border-danger/50 bg-danger/5" : "border-edge bg-raised"
      }`}
    >
      <div className="relative flex h-16 items-center justify-center bg-obsidian">
        {thumbUrl ? (
          <img src={thumbUrl} alt="" className="h-full w-full object-cover" />
        ) : item.media?.kind === "audio" || item.file.type.startsWith("audio/") ? (
          <Music size={16} className="text-sage" />
        ) : item.media?.kind === "video" || item.file.type.startsWith("video/") ? (
          <Video size={16} className="text-sage" />
        ) : (
          <FileText size={16} className="text-sage" />
        )}
        {uploading && (
          <div
            className="absolute inset-0 flex items-center justify-center bg-obsidian/70"
            role="progressbar"
            aria-valuenow={Math.round(item.progress * 100)}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <span className="font-mono text-2xs text-copper">{Math.round(item.progress * 100)}%</span>
          </div>
        )}
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${item.file.name}`}
          className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-sm bg-obsidian/80 text-faint transition-colors hover:text-danger"
        >
          <X size={11} />
        </button>
      </div>
      <div className="flex items-center gap-1 px-1.5 py-1">
        <span className="truncate text-3xs text-dim" title={item.file.name}>
          {item.file.name}
        </span>
        <span className="shrink-0 font-mono text-3xs text-faint">{formatBytes(item.file.size)}</span>
      </div>
      {failed && (
        <button
          type="button"
          onClick={onRetry}
          className="border-t border-danger/30 px-1.5 py-1 text-left text-3xs text-danger transition-colors hover:bg-danger/10"
        >
          {item.error} · retry
        </button>
      )}
    </div>
  );
}
