import { useEffect, useRef } from "react";
import { ChevronRight, FileCode2, FileText, Globe, Shapes } from "lucide-react";
import { ARTIFACT_LABEL, lineCount, type ArtifactKind, type ChatArtifact } from "../../lib/artifacts";
import { useUI } from "../../store/ui";
import { TypingDots } from "./TypingDots";

export const ARTIFACT_ICON: Record<ArtifactKind, typeof Globe> = {
  html: Globe,
  svg: Shapes,
  markdown: FileText,
  code: FileCode2,
};

export function ArtifactCard({ artifact, pending }: { artifact: ChatArtifact; pending: boolean }) {
  const active = useUI((state) => state.artifact?.id === artifact.id);
  const open = useUI((state) => state.openArtifact);
  const wasPending = useRef(pending);
  const Icon = ARTIFACT_ICON[artifact.kind];
  const lines = lineCount(artifact.content);

  useEffect(() => {
    if (wasPending.current && !pending && window.matchMedia("(min-width: 1280px)").matches) {
      open(artifact);
    }
    wasPending.current = pending;
  }, [pending, artifact, open]);

  return (
    <button
      type="button"
      onClick={() => {
        if (!pending) open(artifact);
      }}
      aria-busy={pending}
      className={`my-3 flex w-full items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition-colors ${
        active
          ? "border-copper/50 bg-copper-tint"
          : "border-line bg-surface hover:border-copper/40 hover:bg-raised-soft"
      }`}
    >
      <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-line bg-raised-soft text-copper">
        <Icon size={18} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold text-ink">{artifact.title}</span>
        <span className="mt-0.5 flex items-center gap-1.5 text-2xs text-muted">
          {pending ? (
            <>
              <TypingDots className="text-copper" /> Writing · {lines} lines
            </>
          ) : (
            <>
              {ARTIFACT_LABEL[artifact.kind]} · {lines} lines
            </>
          )}
        </span>
      </span>
      {!pending && (
        <span className="flex shrink-0 items-center gap-0.5 text-2xs font-medium text-copper">
          Open <ChevronRight size={14} />
        </span>
      )}
    </button>
  );
}
