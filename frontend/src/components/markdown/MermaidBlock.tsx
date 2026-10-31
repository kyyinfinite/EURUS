/** MermaidBlock — renders ```mermaid fences as diagrams.
 *
 *  Mermaid is dynamically imported on first use and runs with
 *  securityLevel "strict" (labels escaped, no HTML injection). While the
 *  surrounding turn is streaming, the block stays in source form and renders
 *  once the fence closes — partial syntax never reaches the layout engine.
 *  On a diagram error the source is shown with the parser message.
 */

import { memo, useEffect, useRef, useState } from "react";
import { AlertTriangle, DiagramIcon, Loader2 } from "./mermaidIcons";

let mermaidReady: Promise<typeof import("mermaid").default> | null = null;

async function getMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then((module) => {
      const mermaid = module.default;
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        theme: "base",
        themeVariables: {
          background: "#FFFFFF",
          primaryColor: "#F6EDE5",
          primaryTextColor: "#332A24",
          primaryBorderColor: "#995329",
          lineColor: "#B0703F",
          secondaryColor: "#EDE7DF",
          tertiaryColor: "#F7F5F1",
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: "13px",
        },
      });
      return mermaid;
    });
    mermaidReady.catch(() => {
      mermaidReady = null;
    });
  }
  return mermaidReady;
}

let renderSeq = 0;

function MermaidBlockImpl({ code, pending }: { code: string; pending?: boolean }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (pending) return;
    let cancelled = false;
    setRendering(true);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      void (async () => {
        try {
          const mermaid = await getMermaid();
          const id = `eurus-mermaid-${++renderSeq}`;
          const { svg: rendered } = await mermaid.render(id, code);
          if (!cancelled) {
            setSvg(rendered);
            setError(null);
          }
        } catch (exc) {
          if (!cancelled) {
            setError(exc instanceof Error ? exc.message : "diagram could not be rendered");
            setSvg(null);
          }
        } finally {
          if (!cancelled) setRendering(false);
        }
      })();
    }, 120);
    return () => {
      cancelled = true;
    };
  }, [code, pending]);

  if (pending) {
    return (
      <div className="my-4 flex items-center gap-2.5 rounded-xl border border-dashed border-copper/40 bg-copper-tint px-4 py-6 text-[13px] text-muted">
        <Loader2 size={15} className="animate-spin text-copper" />
        Drawing diagram as the response streams…
      </div>
    );
  }

  if (error) {
    return (
      <div className="my-4 overflow-hidden rounded-xl border border-warn/30 bg-warn-soft">
        <div className="flex items-center gap-2 border-b border-warn/20 px-4 py-2 text-[13px] font-medium text-warn">
          <AlertTriangle size={14} /> Mermaid diagram error
        </div>
        <pre className="overflow-x-auto px-4 py-3 font-mono text-xs leading-relaxed text-dim">
          <code>{code}</code>
        </pre>
        <p className="border-t border-warn/20 px-4 py-2 text-xs text-muted">{error}</p>
      </div>
    );
  }

  return (
    <figure className="my-4 overflow-hidden rounded-xl border border-line bg-surface shadow-card">
      <div className="flex items-center gap-2 border-b border-line-soft bg-raised-soft/70 px-3.5 py-1.5 text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
        <DiagramIcon /> mermaid diagram
        {rendering && <Loader2 size={11} className="animate-spin" />}
      </div>
      <div className="flex justify-center overflow-x-auto p-4">
        {svg ? (
          // Rendered locally by mermaid (securityLevel: "strict") from the
          // fenced source; not untrusted HTML from the network.
          <div
            className="mermaid-svg [&_svg]:max-w-full"
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        ) : (
          <div className="flex items-center gap-2 py-6 text-[13px] text-muted">
            {!rendering && <Loader2 size={14} className="animate-spin text-copper" />}
            Rendering diagram…
          </div>
        )}
      </div>
    </figure>
  );
}

export const MermaidBlock = memo(MermaidBlockImpl);
