
import { useEffect, useState } from "react";
import { api, fetchBlobUrl } from "./api";
import type { AttachmentRef, MediaKind, MediaView } from "./types";

const MARKER = /\[attach id=([a-f0-9]{32}) kind=(image|audio|video|document) name=(\S+) mime=(\S+) size=(\d+)\]/g;
const GENERATED_MARKER = /\[(image|video|voice):([a-f0-9]{32})\]/g;

const GENERATED_KIND_BY_MARKER: Record<string, MediaKind> = {
  image: "image",
  video: "video",
  voice: "audio",
};

export function decodeMarkerValue(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function sanitizeImageSource(source: string | undefined): string | null {
  if (!source) return null;
  const trimmed = source.trim();
  if (!trimmed || trimmed.startsWith("//")) return null;
  if (trimmed.startsWith("/")) {
    try {
      const base = new URL("https://eurus.invalid");
      const parsed = new URL(trimmed, base);
      return parsed.origin === base.origin ? trimmed : null;
    } catch {
      return null;
    }
  }
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return null;
    return parsed.href;
  } catch {
    return null;
  }
}

export interface MessageSegment {
  type: "text" | "attachment" | "generated";
  text?: string;
  attachment?: AttachmentRef;
  generated?: { id: string; kind: MediaKind };
}

interface MarkerMatch {
  index: number;
  length: number;
  segment: MessageSegment;
}

function collectMatches(content: string): MarkerMatch[] {
  const matches: MarkerMatch[] = [];
  MARKER.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = MARKER.exec(content)) !== null) {
    matches.push({
      index: match.index,
      length: match[0].length,
      segment: {
        type: "attachment",
        attachment: {
          id: match[1],
          kind: match[2] as MediaKind,
          name: decodeMarkerValue(match[3]),
          mime: decodeMarkerValue(match[4]),
          size: Number(match[5]),
        },
      },
    });
  }
  GENERATED_MARKER.lastIndex = 0;
  while ((match = GENERATED_MARKER.exec(content)) !== null) {
    matches.push({
      index: match.index,
      length: match[0].length,
      segment: {
        type: "generated",
        generated: {
          id: match[2],
          kind: GENERATED_KIND_BY_MARKER[match[1]] ?? "document",
        },
      },
    });
  }
  return matches.sort((left, right) => left.index - right.index);
}

export function parseMessageContent(content: string): MessageSegment[] {
  if (!content) return [];
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  for (const match of collectMatches(content)) {
    if (match.index < lastIndex) continue;
    if (match.index > lastIndex) {
      segments.push({ type: "text", text: content.slice(lastIndex, match.index) });
    }
    segments.push(match.segment);
    lastIndex = match.index + match.length;
  }
  if (lastIndex < content.length) {
    segments.push({ type: "text", text: content.slice(lastIndex) });
  }
  return segments;
}

const CODE_EXTENSIONS = [
  "py", "js", "mjs", "cjs", "ts", "jsx", "tsx", "html", "htm", "css", "sql",
  "sh", "bash", "yaml", "yml", "toml", "rst", "c", "h", "cpp", "hpp", "java",
  "go", "rs", "rb", "php", "swift", "kt",
] as const;

export function kindFromFile(file: File): MediaKind | null {
  const type = file.type.toLowerCase();
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("audio/")) return "audio";
  if (type.startsWith("video/")) return "video";
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "webp", "gif"].includes(ext)) return "image";
  if (["mp3", "wav", "ogg", "m4a", "weba"].includes(ext)) return "audio";
  if (["mp4", "webm", "mov"].includes(ext)) return "video";
  if (["pdf", "txt", "md", "csv", "json", "xml", "docx", "xlsx"].includes(ext)) return "document";
  if ((CODE_EXTENSIONS as readonly string[]).includes(ext)) return "document";
  if (type === "text/plain" || type === "application/json" || type.startsWith("text/")) {
    return "document";
  }
  return null;
}

export const ACCEPT_BY_KIND: Record<string, string> = {
  image: "image/png,image/jpeg,image/webp,image/gif",
  document:
    ".pdf,.txt,.md,.csv,.json,.xml,.docx,.xlsx,.py,.js,.ts,.tsx,.jsx,.html,.css,.sql,.sh,.yaml,.yml,.toml,.c,.cpp,.java,.go,.rs,application/pdf,text/plain,text/markdown,text/csv,application/json,text/xml",
  audio: "audio/*,.mp3,.wav,.ogg,.m4a,.weba",
  video: "video/mp4,video/webm,video/quicktime,.mp4,.webm,.mov",
};

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;


const blobCache = new Map<string, string>();
const pending = new Map<string, Promise<string>>();
const metaCache = new Map<string, MediaView>();
const pendingMeta = new Map<string, Promise<MediaView>>();

export function loadMediaMeta(mediaId: string): Promise<MediaView> {
  const cached = metaCache.get(mediaId);
  if (cached) return Promise.resolve(cached);
  const inFlight = pendingMeta.get(mediaId);
  if (inFlight) return inFlight;
  const promise = api
    .get<MediaView>(`/api/media/${mediaId}`)
    .then((view) => {
      metaCache.set(mediaId, view);
      pendingMeta.delete(mediaId);
      return view;
    })
    .catch((error: Error) => {
      pendingMeta.delete(mediaId);
      throw error;
    });
  pendingMeta.set(mediaId, promise);
  return promise;
}

export function useMediaAttachment(
  mediaId: string | null,
  kindHint: MediaKind,
): { attachment: AttachmentRef | null; error: string | null } {
  const fallback: AttachmentRef | null = mediaId
    ? { id: mediaId, kind: kindHint, name: kindHint, mime: `${kindHint}/unknown`, size: 0 }
    : null;
  const [attachment, setAttachment] = useState<AttachmentRef | null>(fallback);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!mediaId) {
      setAttachment(null);
      setError(null);
      return;
    }
    const cached = metaCache.get(mediaId);
    if (cached) {
      setAttachment({
        id: cached.id,
        kind: cached.kind,
        name: cached.filename,
        mime: cached.mime,
        size: cached.size,
      });
      setError(null);
      return;
    }
    setAttachment({ id: mediaId, kind: kindHint, name: kindHint, mime: `${kindHint}/unknown`, size: 0 });
    setError(null);
    let cancelled = false;
    loadMediaMeta(mediaId)
      .then((view) => {
        if (cancelled) return;
        setAttachment({
          id: view.id,
          kind: view.kind,
          name: view.filename,
          mime: view.mime,
          size: view.size,
        });
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });
    return () => {
      cancelled = true;
    };
  }, [mediaId, kindHint]);

  return { attachment, error };
}

export function loadMediaUrl(mediaId: string, path: string): Promise<string> {
  const cached = blobCache.get(mediaId);
  if (cached) return Promise.resolve(cached);
  const inFlight = pending.get(mediaId);
  if (inFlight) return inFlight;
  const promise = fetchBlobUrl(path)
    .then((url) => {
      blobCache.set(mediaId, url);
      pending.delete(mediaId);
      return url;
    })
    .catch((error) => {
      pending.delete(mediaId);
      throw error;
    });
  pending.set(mediaId, promise);
  return promise;
}

export function useMediaUrl(attachment: AttachmentRef | null): {
  url: string | null;
  error: string | null;
} {
  const [url, setUrl] = useState<string | null>(attachment ? blobCache.get(attachment.id) ?? null : null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!attachment) {
      setUrl(null);
      return;
    }
    let cancelled = false;
    const cached = blobCache.get(attachment.id);
    if (cached) {
      setUrl(cached);
      setError(null);
      return;
    }
    setUrl(null);
    setError(null);
    loadMediaUrl(attachment.id, `/api/media/${attachment.id}/content`)
      .then((resolved) => {
        if (!cancelled) setUrl(resolved);
      })
      .catch((exc: Error) => {
        if (!cancelled) setError(exc.message);
      });
    return () => {
      cancelled = true;
    };
  }, [attachment]);

  return { url, error };
}
