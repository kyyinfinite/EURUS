
import { useEffect, useState } from "react";
import { fetchBlobUrl } from "./api";
import type { AttachmentRef, MediaKind } from "./types";

const MARKER = /\[attach id=([a-f0-9]{32}) kind=(image|audio|video|document) name=(\S+) mime=(\S+) size=(\d+)\]/g;

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
  type: "text" | "attachment";
  text?: string;
  attachment?: AttachmentRef;
}

export function parseMessageContent(content: string): MessageSegment[] {
  if (!content) return [];
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  MARKER.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = MARKER.exec(content)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: "text", text: content.slice(lastIndex, match.index) });
    }
    segments.push({
      type: "attachment",
      attachment: {
        id: match[1],
        kind: match[2] as MediaKind,
        name: decodeMarkerValue(match[3]),
        mime: decodeMarkerValue(match[4]),
        size: Number(match[5]),
      },
    });
    lastIndex = match.index + match[0].length;
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
  if (type.startsWith("video/")) {
    const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (ext === "mov") return null;
    return "video";
  }
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "webp", "gif"].includes(ext)) return "image";
  if (["mp3", "wav", "ogg", "m4a", "weba"].includes(ext)) return "audio";
  if (["mp4", "webm"].includes(ext)) return "video";
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
  video: "video/mp4,video/webm",
};

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;


const blobCache = new Map<string, string>();
const pending = new Map<string, Promise<string>>();

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
