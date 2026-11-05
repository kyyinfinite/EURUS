import { useMemo, useState } from "react";
import { Check, Copy, Download, X } from "lucide-react";
import {
  ARTIFACT_LABEL,
  artifactFileName,
  lineCount,
  previewDocument,
  type ChatArtifact,
} from "../../lib/artifacts";
import { useUI } from "../../store/ui";
import { Drawer, IconButton } from "../ui";
import { CodeBlock } from "../markdown/CodeBlock";
import { Markdown } from "../markdown/Markdown";
import { ARTIFACT_ICON } from "./ArtifactCard";

export function ArtifactPane({ artifact }: { artifact: ChatArtifact }) {
  const close = useUI((state) => state.closeArtifact);
  const previewable = artifact.kind !== "code";
  const [tab, setTab] = useState<"preview" | "code">(previewable ? "preview" : "code");
  const [copied, setCopied] = useState(false);
  const srcDoc = useMemo(
    () => (artifact.kind === "html" || artifact.kind === "svg" ? previewDocument(artifact) : ""),
    [artifact],
  );
  const Icon = ARTIFACT_ICON[artifact.kind];

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(artifact.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  const download = () => {
    const blob = new Blob([artifact.content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = artifactFileName(artifact);
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b border-line px-3 py-2.5">
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-line bg-raised-soft text-copper">
          <Icon size={15} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-semibold text-ink">{artifact.title}</p>
          <p className="truncate text-2xs text-muted">
            {ARTIFACT_LABEL[artifact.kind]} · {lineCount(artifact.content)} lines
          </p>
        </div>
        {previewable && (
          <div className="flex rounded-lg border border-line bg-raised-soft p-0.5 text-xs">
            {(["preview", "code"] as const).map((id) => (
              <button
                key={id}
                type="button"
                onClick={() => setTab(id)}
                className={`rounded-md px-2.5 py-1 font-medium capitalize transition-colors ${
                  tab === id ? "bg-surface text-copper-dark shadow-panel" : "text-muted hover:text-ink"
                }`}
              >
                {id}
              </button>
            ))}
          </div>
        )}
        <IconButton label={copied ? "Copied" : "Copy"} onClick={() => void copy()} size={32}>
          {copied ? <Check size={14} className="text-ok" /> : <Copy size={14} className="text-muted" />}
        </IconButton>
        <IconButton label="Download" onClick={download} size={32}>
          <Download size={14} className="text-muted" />
        </IconButton>
        <IconButton label="Close" onClick={close} size={32}>
          <X size={14} className="text-muted" />
        </IconButton>
      </header>
      <div className="min-h-0 flex-1">
        {tab === "preview" && (artifact.kind === "html" || artifact.kind === "svg") ? (
          <iframe
            title={artifact.title}
            sandbox="allow-scripts"
            srcDoc={srcDoc}
            className="h-full w-full border-0 bg-white"
          />
        ) : tab === "preview" ? (
          <div className="h-full overflow-y-auto p-5">
            <Markdown text={artifact.content} />
          </div>
        ) : (
          <div className="h-full overflow-y-auto p-3">
            <CodeBlock code={artifact.content} lang={artifact.lang} defaultExpanded />
          </div>
        )}
      </div>
    </div>
  );
}

export function ArtifactDrawer() {
  const artifact = useUI((state) => state.artifact);
  const close = useUI((state) => state.closeArtifact);
  return (
    <Drawer open={artifact !== null} onClose={close} side="right" widthClass="w-full sm:w-[560px]">
      {artifact && <ArtifactPane key={artifact.id} artifact={artifact} />}
    </Drawer>
  );
}
