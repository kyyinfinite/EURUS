/** CodeBlock — the canonical code surface of the intelligence renderer.
 *
 *  Features (brief §6): language identification, Shiki syntax highlighting,
 *  copy, download, expand/collapse, optional line numbers, horizontal
 *  scrolling, readable light styling.
 *
 *  Streaming-safe: while the surrounding turn streams, the *trailing* open
 *  fence renders as plain text (via the pending flag) and completed blocks
 *  are memoised so they never re-highlight on later tokens.
 */

import { memo, useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronUp, Copy, Download, ListOrdered, Loader2 } from "lucide-react";
import { highlightCode } from "./highlight";
import { extForLang, normalizeLang } from "./streaming";

const LINE_NUMBER_PREF = "eurus.ui.codeLineNumbers";

function readLineNumbersPref(): boolean {
  try {
    return localStorage.getItem(LINE_NUMBER_PREF) === "1";
  } catch {
    return false;
  }
}

function writeLineNumbersPref(value: boolean): void {
  try {
    localStorage.setItem(LINE_NUMBER_PREF, value ? "1" : "0");
  } catch {
    /* preference is best-effort */
  }
}

const COLLAPSE_THRESHOLD = 16; // lines beyond which a block starts collapsed

function CodeBlockImpl({
  code,
  lang: rawLang,
  pending = false,
  mermaid = false,
}: {
  code: string;
  lang: string;
  pending?: boolean;
  mermaid?: boolean;
}) {
  const lang = normalizeLang(rawLang);
  const [html, setHtml] = useState<string | null>(null);
  const [highlighting, setHighlighting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [lineNumbers, setLineNumbers] = useState(readLineNumbersPref);
  const timerRef = useRef<number | null>(null);

  // Highlight with a small debounce so streaming tokens do not thrash Shiki.
  useEffect(() => {
    if (pending || mermaid) {
      setHtml(null);
      return;
    }
    let cancelled = false;
    setHighlighting(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(
      () => {
        void highlightCode(code, lang).then((result) => {
          if (cancelled) return;
          setHtml(result);
          setHighlighting(false);
        });
      },
      pending ? 400 : 0,
    );
    return () => {
      cancelled = true;
    };
  }, [code, lang, pending, mermaid]);

  const lineCount = useMemo(() => code.replace(/\n$/, "").split("\n").length, [code]);
  const collapsible = lineCount > COLLAPSE_THRESHOLD;
  const effectiveExpanded = expanded || !collapsible;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable */
    }
  };

  const download = () => {
    const blob = new Blob([code], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `eurus-snippet.${extForLang(lang)}`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 4000);
  };

  const toggleLineNumbers = () => {
    const next = !lineNumbers;
    setLineNumbers(next);
    writeLineNumbersPref(next);
  };

  return (
    <div className="group/code my-4 overflow-hidden rounded-xl border border-line bg-surface shadow-card">
      {/* header */}
      <div className="flex items-center justify-between gap-2 border-b border-line-soft bg-raised-soft/70 pl-3.5 pr-2 py-1.5">
        <span className="flex min-w-0 items-center gap-2">
          <span className="flex gap-1.5" aria-hidden="true">
            <span className="h-2.5 w-2.5 rounded-full bg-copper/30" />
            <span className="h-2.5 w-2.5 rounded-full bg-bronze/30" />
            <span className="h-2.5 w-2.5 rounded-full bg-line" />
          </span>
          <span className="truncate font-mono text-[11px] font-medium uppercase tracking-[0.08em] text-muted">
            {mermaid ? "mermaid" : lang}
          </span>
          {pending && (
            <span className="flex shrink-0 items-center gap-1 text-[11px] font-medium text-copper">
              <Loader2 size={11} className="animate-spin" /> writing…
            </span>
          )}
          {highlighting && !pending && (
            <Loader2 size={11} className="shrink-0 animate-spin text-faint" aria-label="Highlighting" />
          )}
        </span>
        <span className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            onClick={toggleLineNumbers}
            aria-label={lineNumbers ? "Hide line numbers" : "Show line numbers"}
            title={lineNumbers ? "Hide line numbers" : "Show line numbers"}
            className={`flex h-7 w-7 items-center justify-center rounded-md transition-colors ${
              lineNumbers ? "bg-copper-soft text-copper-dark" : "text-faint hover:bg-raised-soft hover:text-ink"
            }`}
          >
            <ListOrdered size={13} />
          </button>
          <button
            type="button"
            onClick={copy}
            aria-label="Copy code"
            title="Copy code"
            className="flex h-7 w-7 items-center justify-center rounded-md text-faint transition-colors hover:bg-raised-soft hover:text-ink"
          >
            {copied ? <Check size={13} className="text-ok" /> : <Copy size={13} />}
          </button>
          <button
            type="button"
            onClick={download}
            aria-label="Download snippet"
            title="Download snippet"
            className="flex h-7 w-7 items-center justify-center rounded-md text-faint transition-colors hover:bg-raised-soft hover:text-ink"
          >
            <Download size={13} />
          </button>
        </span>
      </div>

      {/* body */}
      <div className="relative">
        <div
          className={`overflow-x-auto ${
            effectiveExpanded ? "" : "max-h-[380px] overflow-y-hidden"
          }`}
        >
          {html ? (
            <div className="code-highlighted !bg-transparent text-[13px] leading-[1.6]" data-lang={lang}>
              <ShikiContainer html={html} lineNumbers={lineNumbers && !pending} lineCount={lineCount} />
            </div>
          ) : (
            <pre className="!bg-transparent px-4 py-3.5 text-[13px] leading-[1.6]">
              <code className="font-mono text-ink">{code}</code>
            </pre>
          )}
        </div>
        {collapsible && !expanded && (
          <div
            className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-surface to-transparent"
            aria-hidden="true"
          />
        )}
        {collapsible && (
          <div className="flex justify-center border-t border-line-soft bg-surface/95 py-1.5">
            <button
              type="button"
              onClick={() => setExpanded((value) => !value)}
              className="flex items-center gap-1 rounded-full border border-line bg-surface px-3 py-1 text-[11px] font-semibold text-muted transition-colors hover:border-copper/40 hover:text-copper-dark"
            >
              {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
              {expanded ? "Collapse" : `Expand (${lineCount} lines)`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Wrap Shiki output with an optional line-number gutter. */
function ShikiContainer({
  html,
  lineNumbers,
  lineCount,
}: {
  html: string;
  lineNumbers: boolean;
  lineCount: number;
}) {
  if (!lineNumbers) {
    // Shiki output is generated locally from the code text by the Shiki
    // engine (theme github-light) — it is not user HTML.
    return <div className="shiki-wrap px-4 py-3.5 [&_pre]:!bg-transparent [&_pre]:!m-0" dangerouslySetInnerHTML={{ __html: html }} />;
  }
  return (
    <div className="flex px-0 py-0">
      <ol
        className="sticky left-0 shrink-0 select-none border-r border-line-soft bg-raised-soft/40 py-3.5 pl-3.5 pr-2.5 text-right font-mono text-[12px] leading-[1.6] text-faint"
        aria-hidden="true"
      >
        {Array.from({ length: lineCount }, (_, index) => (
          <li key={index}>{index + 1}</li>
        ))}
      </ol>
      <div className="min-w-0 flex-1 px-4 py-3.5 [&_pre]:!bg-transparent [&_pre]:!m-0" dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}

export const CodeBlock = memo(CodeBlockImpl);
