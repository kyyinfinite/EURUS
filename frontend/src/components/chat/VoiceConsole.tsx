import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, MicOff, PhoneOff, Radio } from "lucide-react";
import { api } from "../../lib/api";
import { resolveToken } from "../../lib/token";
import { Badge, Button } from "../ui";

interface RealtimeSessionResponse {
  bridge_id: string;
  model: string;
  voice: string;
  sample_rate: number;
  format: string;
  expires_in: number;
}

type VoiceStatus = "idle" | "connecting" | "listening" | "speaking" | "error" | "closed";

export function VoiceConsole({
  sessionId,
  onClose,
}: {
  sessionId: string;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<VoiceStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [userTranscript, setUserTranscript] = useState("");
  const [assistantTranscript, setAssistantTranscript] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const playHeadRef = useRef(0);

  const stopCapture = useCallback(() => {
    const processor = processorRef.current;
    if (processor) processor.disconnect();
    processorRef.current = null;
    const stream = streamRef.current;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;

    const start = async () => {
      try {
        const session = await api.post<RealtimeSessionResponse>("/api/realtime/session", {
          session_id: sessionId,
        });
        if (cancelled) return;
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        socket = new WebSocket(
          `${protocol}://${window.location.host}/api/realtime/stream/${session.bridge_id}?token=${encodeURIComponent(resolveToken() ?? "")}`,
        );
        socket.binaryType = "arraybuffer";
        socketRef.current = socket;
        const active = socket;
        socket.onopen = () => {
          setStatus("listening");
        };
        socket.onerror = () => {
          setStatus("error");
          setError("voice connection failed");
        };
        socket.onclose = () => {
          setStatus((current) => (current === "error" ? current : "closed"));
          stopCapture();
        };
        socket.onmessage = (event) => {
          if (typeof event.data !== "string") return;
          try {
            const message = JSON.parse(event.data) as Record<string, unknown>;
            const kind = String(message.type ?? "");
            if (kind === "session.ready") {
              void beginCapture(active).catch(() => undefined);
            } else if (kind === "user_transcript") {
              setUserTranscript(String(message.text ?? ""));
              setAssistantTranscript("");
            } else if (kind === "assistant_transcript_delta") {
              setAssistantTranscript((current) => current + String(message.delta ?? ""));
            } else if (kind === "response.audio.delta" || kind === "assistant_audio_delta") {
              playPcm16Chunk(String(message.audio ?? ""));
            } else if (kind === "assistant_audio_done") {
              playHeadRef.current = 0;
            } else if (kind === "user_speech_started") {
              setStatus("listening");
              setAssistantTranscript("");
              playHeadRef.current = 0;
            } else if (kind === "response_done") {
              setStatus("listening");
            } else if (kind === "error") {
              setError(String(message.detail ?? "realtime error"));
            }
          } catch {
            void 0;
          }
        };
      } catch (exc) {
        setStatus("error");
        setError(exc instanceof Error ? exc.message : "failed to start voice session");
      }
    };

    const beginCapture = async (target: WebSocket) => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
        });
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = stream;
        const context = new AudioContext({ sampleRate: 24000 });
        audioContextRef.current = context;
        const source = context.createMediaStreamSource(stream);
        const processor = context.createScriptProcessor(4096, 1, 1);
        processorRef.current = processor;
        processor.onaudioprocess = (audioEvent) => {
          if (target.readyState !== WebSocket.OPEN) return;
          const input = audioEvent.inputBuffer.getChannelData(0);
          const pcm = floatToPcm16(input);
          if (pcm.byteLength > 0) target.send(pcm);
        };
        source.connect(processor);
        processor.connect(context.destination);
      } catch (exc) {
        setStatus("error");
        setError(exc instanceof Error ? exc.message : "microphone unavailable");
      }
    };

    const playPcm16Chunk = (base64: string) => {
      if (!base64) return;
      setStatus("speaking");
      const context = audioContextRef.current;
      if (!context) return;
      try {
        const bytes = base64ToBytes(base64);
        const samples = pcm16ToFloat32(bytes);
        const buffer = context.createBuffer(1, samples.length, 24000);
        buffer.getChannelData(0).set(samples);
        const sourceNode = context.createBufferSource();
        sourceNode.buffer = buffer;
        sourceNode.connect(context.destination);
        const now = context.currentTime;
        const startAt = Math.max(now, playHeadRef.current);
        playHeadRef.current = startAt + buffer.duration;
        sourceNode.start(startAt);
      } catch {
        void 0;
      }
    };

    void start();

    return () => {
      cancelled = true;
      const current = socketRef.current;
      if (current && current.readyState !== WebSocket.CLOSED) current.close();
      socketRef.current = null;
      stopCapture();
      void audioContextRef.current?.close().catch(() => undefined);
      audioContextRef.current = null;
    };
  }, [sessionId, stopCapture]);

  const interrupt = () => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "interrupt" }));
      setAssistantTranscript("");
      playHeadRef.current = 0;
    }
  };

  const hangUp = () => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) socket.close();
    stopCapture();
    setStatus("closed");
    onClose();
  };

  const statusLabel: Record<VoiceStatus, string> = {
    idle: "Idle",
    connecting: "Connecting",
    listening: "Listening",
    speaking: "Speaking",
    error: "Error",
    closed: "Ended",
  };

  return (
    <div className="fixed inset-x-0 bottom-0 z-[70] border-t border-copper/25 bg-surface shadow-overlay safe-bottom">
      <div className="mx-auto flex max-w-3xl flex-col gap-3 px-4 py-4">
        <div className="flex items-center gap-3">
          <span
            className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-full border ${
              status === "speaking"
                ? "border-copper/50 bg-copper-soft text-copper-dark"
                : "border-line bg-raised-soft text-muted"
            }`}
          >
            {status === "connecting" ? (
              <Loader2 size={16} className="animate-spin text-copper" />
            ) : status === "speaking" ? (
              <Radio size={16} className="animate-pulse-soft" />
            ) : (
              <Mic size={16} />
            )}
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-ink">Voice mode</p>
            <p className="text-2xs text-muted">
              {statusLabel[status]}
              {error ? ` — ${error}` : ""}
            </p>
          </div>
          <Badge tone={status === "error" ? "danger" : status === "speaking" ? "warn" : "ok"}>
            {statusLabel[status]}
          </Badge>
          <Button variant="ghost" size="sm" onClick={interrupt} disabled={status !== "speaking"}>
            <MicOff size={14} /> Barge in
          </Button>
          <Button variant="danger" size="sm" onClick={hangUp}>
            <PhoneOff size={14} /> End
          </Button>
        </div>
        <div className="max-h-28 space-y-1.5 overflow-auto rounded-xl border border-line bg-canvas px-3.5 py-2.5">
          {userTranscript ? (
            <p className="text-[13px] leading-relaxed text-ink">
              <span className="mr-1.5 font-mono text-2xs uppercase text-muted">you</span>
              {userTranscript}
            </p>
          ) : null}
          {assistantTranscript ? (
            <p className="text-[13px] leading-relaxed text-dim">
              <span className="mr-1.5 font-mono text-2xs uppercase text-copper">eurus</span>
              {assistantTranscript}
            </p>
          ) : null}
          {!userTranscript && !assistantTranscript ? (
            <p className="text-xs text-faint">Start talking — transcripts appear here.</p>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function floatToPcm16(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let index = 0; index < input.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, input[index]));
    view.setInt16(index * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  }
  return buffer;
}

function pcm16ToFloat32(bytes: Uint8Array): Float32Array {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const count = Math.floor(bytes.byteLength / 2);
  const samples = new Float32Array(count);
  for (let index = 0; index < count; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32768;
  }
  return samples;
}

function base64ToBytes(base64: string): Uint8Array {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}
