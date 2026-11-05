
import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AudioLines,
  EyeOff,
  FileText,
  ImageIcon,
  Mic,
  Paperclip,
  Send,
  Square,
  Upload,
  Video,
} from "lucide-react";
import { uploadCancellable } from "../../lib/api";
import { ACCEPT_BY_KIND, kindFromFile, MAX_UPLOAD_BYTES } from "../../lib/media";
import type { MediaView, ProviderView } from "../../lib/types";
import { useUI } from "../../store/ui";
import { Spinner } from "../ui";
import { AttachmentPreview, type PendingAttachment } from "../media/MediaBlocks";

let chipSeq = 1;

export interface ChatComposerProps {
  sessionId: string | null;
  onEnsureSession?: () => Promise<string>;
  onSend: (text: string, attachments: MediaView[]) => Promise<void> | void;
  onCancel?: () => void;
  disabled?: boolean;
  busy?: boolean;
  running?: boolean;
  placeholder?: string;
  variant?: "default" | "hero";
  autoFocus?: boolean;
  maxRows?: number;
}

export function ChatComposer({
  sessionId,
  onEnsureSession,
  onSend,
  onCancel,
  disabled = false,
  busy = false,
  running = false,
  placeholder = "Message EURUS…",
  variant = "default",
  autoFocus = false,
  maxRows = 8,
}: ChatComposerProps) {
  const [draft, setDraft] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [menuOpen, setMenuOpen] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [recording, setRecording] = useState(false);
  const [recordingSeconds, setRecordingSeconds] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const imageInput = useRef<HTMLInputElement | null>(null);
  const docInput = useRef<HTMLInputElement | null>(null);
  const audioInput = useRef<HTMLInputElement | null>(null);
  const videoInput = useRef<HTMLInputElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recordingStreamRef = useRef<MediaStream | null>(null);
  const recordingChunksRef = useRef<Blob[]>([]);
  const recordingStartedAtRef = useRef(0);
  const uploadCancelsRef = useRef(new Map<string, () => void>());
  const mountedRef = useRef(true);
  const recordingIntervalRef = useRef<number | null>(null);
  const recordingTimeoutRef = useRef<number | null>(null);
  const pushToast = useUI((state) => state.pushToast);

  const providersQuery = useQuery({
    queryKey: ["providers"],
    queryFn: () => apiGetProviders(),
    staleTime: 60_000,
  });
  const readyProviders = (providersQuery.data ?? []).filter((p) => p.status === "ready");
  const anyMultimodalInput = readyProviders.some(
    (provider) => provider.capabilities?.imageInput || provider.capabilities?.videoInput,
  );
  const hasVisualAttachment = attachments.some(
    (item) => item.media?.kind === "image" || item.file.type.startsWith("image/"),
  );
  const showCapabilityNotice = hasVisualAttachment && !anyMultimodalInput;

  const autoGrow = useCallback(() => {
    const node = textareaRef.current;
    if (!node) return;
    node.style.height = "auto";
    const lineHeight = 22;
    const max = lineHeight * maxRows;
    node.style.height = `${Math.min(node.scrollHeight, max)}px`;
    node.style.overflowY = node.scrollHeight > max ? "auto" : "hidden";
  }, [maxRows]);

  useEffect(autoGrow, [draft, autoGrow]);

  const focusTextarea = useCallback(() => {
    window.requestAnimationFrame(() => textareaRef.current?.focus());
  }, []);

  const stopRecording = useCallback(() => {
    if (recordingIntervalRef.current !== null) {
      window.clearInterval(recordingIntervalRef.current);
      recordingIntervalRef.current = null;
    }
    if (recordingTimeoutRef.current !== null) {
      window.clearTimeout(recordingTimeoutRef.current);
      recordingTimeoutRef.current = null;
    }
    const recorder = recorderRef.current;
    if (recorder?.state === "recording") recorder.stop();
  }, []);

  useEffect(() => {
    const onPointerDown = (event: PointerEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        if (recording) stopRecording();
        focusTextarea();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [focusTextarea, recording, stopRecording]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (recordingIntervalRef.current !== null) window.clearInterval(recordingIntervalRef.current);
      if (recordingTimeoutRef.current !== null) window.clearTimeout(recordingTimeoutRef.current);
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        recorder.onstop = null;
        recorder.stop();
      }
      recordingStreamRef.current?.getTracks().forEach((track) => track.stop());
      recordingStreamRef.current = null;
      for (const cancel of uploadCancelsRef.current.values()) cancel();
      uploadCancelsRef.current.clear();
    };
  }, []);

  const uploadFile = useCallback(
    async (item: PendingAttachment, resolvedSessionId: string) => {
      const form = new FormData();
      form.append("session_id", resolvedSessionId);
      form.append("file", item.file, item.file.name);
      const transfer = uploadCancellable<MediaView>("/api/media", form, (fraction) => {
        if (!mountedRef.current) return;
        setAttachments((prev) =>
          prev.map((entry) =>
            entry.localId === item.localId ? { ...entry, progress: fraction } : entry,
          ),
        );
      });
      uploadCancelsRef.current.set(item.localId, transfer.cancel);
      setAttachments((prev) =>
        prev.map((entry) =>
          entry.localId === item.localId ? { ...entry, cancel: transfer.cancel } : entry,
        ),
      );
      try {
        const media = await transfer.promise;
        uploadCancelsRef.current.delete(item.localId);
        if (!mountedRef.current) return;
        setAttachments((prev) =>
          prev.map((entry) =>
            entry.localId === item.localId
              ? { ...entry, media, error: null, cancel: undefined }
              : entry,
          ),
        );
      } catch (error) {
        uploadCancelsRef.current.delete(item.localId);
        if (!mountedRef.current) return;
        const detail = error instanceof Error ? error.message : "upload failed";
        setAttachments((prev) =>
          prev.map((entry) =>
            entry.localId === item.localId
              ? { ...entry, error: detail, cancel: undefined }
              : entry,
          ),
        );
      }
    },
    [],
  );

  const addFiles = useCallback(
    async (files: FileList | File[]) => {
      const incoming = Array.from(files);
      for (const file of incoming) {
        if (file.size > MAX_UPLOAD_BYTES) {
          pushToast({
            tone: "danger",
            title: "File too large",
            detail: `${file.name} exceeds ${Math.round(MAX_UPLOAD_BYTES / (1024 * 1024))} MB`,
          });
          continue;
        }
        if (!kindFromFile(file)) {
          pushToast({
            tone: "warn",
            title: "Unsupported file",
            detail: `${file.name} — supported: images (png, jpg, webp, gif), documents (pdf, txt, md, csv, json, xml, docx, xlsx), code sources (py, ts, js, html, css, sql…), audio (mp3, wav, ogg, m4a) and video (mp4, webm)`,
          });
          continue;
        }
        const item: PendingAttachment = {
          localId: `att${chipSeq++}`,
          file,
          progress: 0,
          error: null,
        };
        setAttachments((prev) => [...prev, item]);
        let target = sessionId;
        if (!target && onEnsureSession) {
          try {
            target = await onEnsureSession();
          } catch (error) {
            if (!mountedRef.current) return;
            const detail = error instanceof Error ? error.message : "could not create session";
            setAttachments((prev) =>
              prev.map((entry) =>
                entry.localId === item.localId ? { ...entry, error: detail } : entry,
              ),
            );
            continue;
          }
          if (!mountedRef.current) return;
        }
        if (!target) {
          setAttachments((prev) =>
            prev.map((entry) =>
              entry.localId === item.localId ? { ...entry, error: "no session" } : entry,
            ),
          );
          continue;
        }
        void uploadFile(item, target);
      }
    },
    [sessionId, onEnsureSession, uploadFile, pushToast],
  );

  const submit = useCallback(() => {
    if (disabled || busy || running) return;
    const text = draft.trim();
    const ready = attachments.filter(
      (item): item is PendingAttachment & { media: MediaView } => Boolean(item.media),
    );
    const pending = attachments.some((item) => !item.media && !item.error);
    if ((!text && ready.length === 0) || pending) return;
    setDraft("");
    setAttachments([]);
    void Promise.resolve().then(() => onSend(text, ready.map((item) => item.media))).catch(() => {
      setDraft(text);
      setAttachments(ready);
      pushToast({
        tone: "danger",
        title: "Could not send the message",
        detail: "The draft was restored to the composer.",
      });
      focusTextarea();
    });
    focusTextarea();
  }, [disabled, busy, running, draft, attachments, onSend, pushToast, focusTextarea]);

  const handlePaste = useCallback(
    (event: React.ClipboardEvent) => {
      const files = Array.from(event.clipboardData.files);
      if (files.length > 0) {
        const supported = files.filter((file) => kindFromFile(file));
        if (supported.length > 0) {
          event.preventDefault();
          void addFiles(supported);
        }
      }
    },
    [addFiles],
  );

  const hero = variant === "hero";
  const sendDisabled =
    disabled || busy || running || (!draft.trim() && attachments.filter((a) => a.media).length === 0);

  return (
    <div
      className={`relative ${hero ? "" : "border-t border-line bg-surface"}`}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={(event) => {
        if (event.currentTarget.contains(event.relatedTarget as Node)) return;
        setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        if (event.dataTransfer.files.length > 0) void addFiles(event.dataTransfer.files);
      }}
    >
      {dragging && (
        <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed border-copper/60 bg-canvas/95 text-sm font-medium text-copper-dark">
          <Upload size={20} />
          Drop files to attach
        </div>
      )}

      {attachments.length > 0 && (
        <div
          className={`flex gap-2 overflow-x-auto pb-2 no-scrollbar ${hero ? "mb-2.5" : "px-3 pt-3 sm:px-4"}`}
          aria-label="Pending attachments"
        >
          {attachments.map((item) => (
            <AttachmentPreview
              key={item.localId}
              item={item}
              onRemove={() => {
                item.cancel?.();
                uploadCancelsRef.current.delete(item.localId);
                setAttachments((prev) => prev.filter((entry) => entry.localId !== item.localId));
                focusTextarea();
              }}
              onRetry={() => {
                setAttachments((prev) =>
                  prev.map((entry) =>
                    entry.localId === item.localId ? { ...entry, error: null, progress: 0 } : entry,
                  ),
                );
                const target = sessionId ?? item.media?.session_id;
                if (target) void uploadFile(item, target);
              }}
            />
          ))}
        </div>
      )}

      {recording && (
        <div className="mx-3 mb-2 flex items-center justify-between rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger sm:mx-4">
          <span className="flex items-center gap-2">
            <span className="h-2 w-2 animate-pulse rounded-full bg-danger" />
            Recording voice note · {recordingSeconds}s / 60s
          </span>
          <button type="button" onClick={stopRecording} className="flex items-center gap-1.5 rounded-lg px-2 py-1 font-semibold hover:bg-danger/10">
            <Square size={11} fill="currentColor" /> Stop
          </button>
        </div>
      )}

      {showCapabilityNotice && (
        <div
          className={`mx-1 mb-2 flex items-start gap-2 rounded-lg border border-warn/30 bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn ${
            hero ? "" : "sm:mx-3"
          }`}
          role="note"
        >
          <EyeOff size={13} className="mt-0.5 shrink-0" />
          Connected providers are text-only today — images are stored and referenced for the agent,
          not visually analyzed.
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
        className={hero ? "" : "px-3 py-3 sm:px-4 safe-bottom"}
      >
        <div
          className={`relative flex items-end gap-2 ${
            hero
              ? "rounded-2xl border border-line bg-surface shadow-card transition-colors focus-within:border-copper/50 focus-within:shadow-float"
              : "rounded-2xl border border-line bg-canvas/70 transition-colors focus-within:border-copper/50"
          } ${hero ? "p-2.5" : "p-2"}`}
        >
          <div className="relative" ref={menuRef}>
            <button
              type="button"
              aria-label="Attach files"
              aria-expanded={menuOpen}
              aria-haspopup="menu"
              onClick={() => setMenuOpen((open) => !open)}
              disabled={disabled}
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-faint transition-colors hover:bg-raised-soft hover:text-copper disabled:opacity-40"
            >
              <Paperclip size={17} />
            </button>
            {menuOpen && (
              <div
                role="menu"
                className="absolute bottom-12 left-0 z-30 w-60 animate-scale-in overflow-hidden rounded-xl border border-line bg-surface p-1.5 shadow-overlay"
                onMouseLeave={() => setMenuOpen(false)}
              >
                <MenuItem
                  icon={<ImageIcon size={15} className="text-copper" />}
                  label="Upload image"
                  hint="PNG · JPG · WebP · GIF"
                  onClick={() => {
                    setMenuOpen(false);
                    imageInput.current?.click();
                    focusTextarea();
                  }}
                />
                <MenuItem
                  icon={<FileText size={15} className="text-copper" />}
                  label="Upload document or code"
                  hint="PDF · MD · CSV · JSON · XML · PY · TS · SQL…"
                  onClick={() => {
                    setMenuOpen(false);
                    docInput.current?.click();
                    focusTextarea();
                  }}
                />
                <MenuItem
                  icon={<AudioLines size={15} className="text-copper" />}
                  label="Attach audio"
                  hint="MP3 · WAV · OGG · M4A"
                  onClick={() => {
                    setMenuOpen(false);
                    audioInput.current?.click();
                    focusTextarea();
                  }}
                />
                <MenuItem
                  icon={<Video size={15} className="text-copper" />}
                  label="Attach video"
                  hint="MP4 · WebM"
                  onClick={() => {
                    setMenuOpen(false);
                    videoInput.current?.click();
                    focusTextarea();
                  }}
                />
                <MenuItem
                  icon={<Mic size={15} className="text-copper" />}
                  label="Record voice note"
                  hint="Uses your browser recorder"
                  onClick={() => {
                    setMenuOpen(false);
                    void startRecording();
                  }}
                />
              </div>
            )}
          </div>

          <textarea
            ref={textareaRef}
            value={draft}
            autoFocus={autoFocus}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
            onPaste={handlePaste}
            rows={1}
            disabled={disabled}
            aria-label="Message input"
            placeholder={placeholder}
            className={`min-h-[40px] w-full resize-none !rounded-xl !border-0 !bg-transparent !px-1.5 !py-2 !ring-0 focus:!border-0 focus:!ring-0 ${
              hero ? "text-[15px]" : "text-sm"
            }`}
          />

          {running ? (
            onCancel ? (
              <button
                type="button"
                onClick={onCancel}
                aria-label="Cancel running agent"
                className="flex h-10 shrink-0 items-center gap-1.5 rounded-xl border border-danger/35 bg-danger-soft px-3.5 text-[13px] font-semibold text-danger transition-colors hover:bg-danger/40 hover:text-white"
              >
                <Square size={12} fill="currentColor" /> Stop
              </button>
            ) : (
              <span className="flex h-10 items-center px-2">
                <Spinner />
              </span>
            )
          ) : (
            <button
              type="submit"
              aria-label="Send message"
              disabled={sendDisabled}
              className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full transition-all ${
                sendDisabled
                  ? "bg-raised text-faint"
                  : "bg-copper text-white shadow-launcher hover:bg-copper-dark active:scale-95"
              }`}
            >
              {busy ? <Spinner size={14} /> : <Send size={16} />}
            </button>
          )}
        </div>

        {hero ? (
          <p className="mt-3 text-center text-xs leading-relaxed text-faint">
            Enter to send · Shift+Enter for a new line · drop or paste files to attach
          </p>
        ) : (
          <p className="mt-2 px-1 text-2xs text-faint">
            Enter to send · Shift+Enter for a new line · paste images to attach
          </p>
        )}
      </form>

      <input
        ref={imageInput}
        type="file"
        accept={ACCEPT_BY_KIND.image}
        multiple
        className="hidden"
        onChange={(event) => {
          if (event.target.files) void addFiles(event.target.files);
          event.target.value = "";
          focusTextarea();
        }}
      />
      <input
        ref={docInput}
        type="file"
        accept={ACCEPT_BY_KIND.document}
        multiple
        className="hidden"
        onChange={(event) => {
          if (event.target.files) void addFiles(event.target.files);
          event.target.value = "";
          focusTextarea();
        }}
      />
      <input
        ref={audioInput}
        type="file"
        accept={ACCEPT_BY_KIND.audio}
        multiple
        className="hidden"
        onChange={(event) => {
          if (event.target.files) void addFiles(event.target.files);
          event.target.value = "";
          focusTextarea();
        }}
      />
      <input
        ref={videoInput}
        type="file"
        accept={ACCEPT_BY_KIND.video}
        multiple
        className="hidden"
        onChange={(event) => {
          if (event.target.files) void addFiles(event.target.files);
          event.target.value = "";
          focusTextarea();
        }}
      />
    </div>
  );

  async function startRecording() {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      pushToast({
        tone: "warn",
        title: "Recording unavailable",
        detail: "This browser does not expose the MediaRecorder API.",
      });
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      const recorder = new MediaRecorder(stream);
      recordingStreamRef.current = stream;
      recorderRef.current = recorder;
      recordingChunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) recordingChunksRef.current.push(event.data);
      };
      recorder.onerror = () => stopRecording();
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        recordingStreamRef.current = null;
        recorderRef.current = null;
        setRecording(false);
        const type = recorder.mimeType || "audio/webm";
        const extension = type.includes("mp4") ? "m4a" : "webm";
        const file = new File(recordingChunksRef.current, `voice-note.${extension}`, { type });
        recordingChunksRef.current = [];
        if (file.size > 0) void addFiles([file]);
        focusTextarea();
      };
      recorder.start(250);
      setRecording(true);
      setRecordingSeconds(0);
      recordingStartedAtRef.current = Date.now();
      recordingIntervalRef.current = window.setInterval(() => {
        const elapsed = Math.floor((Date.now() - recordingStartedAtRef.current) / 1000);
        setRecordingSeconds(Math.min(elapsed, 60));
        if (elapsed >= 60) stopRecording();
      }, 1000);
      recordingTimeoutRef.current = window.setTimeout(stopRecording, 60_000);
    } catch {
      recordingStreamRef.current?.getTracks().forEach((track) => track.stop());
      recordingStreamRef.current = null;
      recorderRef.current = null;
      if (!mountedRef.current) return;
      setRecording(false);
      pushToast({ tone: "danger", title: "Microphone denied", detail: "Grant permission to record." });
    }
  }
}

import { api } from "../../lib/api";

function apiGetProviders(): Promise<ProviderView[]> {
  return api.get<ProviderView[]>("/api/providers");
}

function MenuItem({
  icon,
  label,
  hint,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-raised-soft"
    >
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-line bg-canvas">
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-ink">{label}</span>
        <span className="block truncate text-xs text-faint">{hint}</span>
      </span>
    </button>
  );
}
