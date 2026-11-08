import { blockKey, extForLang } from "../components/markdown/streaming";

export type ArtifactKind = "html" | "svg" | "markdown" | "code";

export interface ChatArtifact {
  id: string;
  kind: ArtifactKind;
  lang: string;
  title: string;
  content: string;
}

export const ARTIFACT_LABEL: Record<ArtifactKind, string> = {
  html: "HTML page",
  svg: "SVG graphic",
  markdown: "Document",
  code: "Code",
};

const MIN_CODE_LINES = 20;
const MIN_DOC_LINES = 14;
const CSP =
  "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'";

export function lineCount(content: string): number {
  return content.replace(/\n+$/, "").split("\n").length;
}

function classify(lang: string, code: string, streaming: boolean): ArtifactKind | null {
  const lines = lineCount(code);
  if (lang === "html" || lang === "htm") {
    if (streaming) return "html";
    return /<!doctype|<html|<body|<style|<script|<svg/i.test(code) || lines >= 10 ? "html" : null;
  }
  if (lang === "svg" || (lang === "xml" && /^\s*<svg[\s>]/i.test(code))) return "svg";
  if (lang === "markdown" && lines >= MIN_DOC_LINES) return "markdown";
  if (lang === "mermaid") return null;
  return lines >= MIN_CODE_LINES ? "code" : null;
}

function titleFor(kind: ArtifactKind, lang: string, code: string): string {
  if (kind === "html") {
    return /<title[^>]*>([^<]{1,80})<\/title>/i.exec(code)?.[1]?.trim() || "HTML page";
  }
  if (kind === "svg") return "SVG graphic";
  if (kind === "markdown") {
    return /^#{1,3}\s+(.{1,80})$/m.exec(code)?.[1]?.trim() || "Document";
  }
  const name = /(?:def|class|function|const|export)\s+([A-Za-z_]\w*)/.exec(code)?.[1];
  return `${name ?? "snippet"}.${extForLang(lang)}`;
}

export function buildArtifact(lang: string, code: string, streaming = false): ChatArtifact | null {
  const kind = classify(lang, code, streaming);
  if (!kind) return null;
  return {
    id: blockKey("art", code),
    kind,
    lang,
    title: titleFor(kind, lang, code),
    content: code,
  };
}

function injectHead(content: string): string {
  const meta = `<meta http-equiv="Content-Security-Policy" content="${CSP}">`;
  if (/<head[^>]*>/i.test(content)) {
    return content.replace(/<head[^>]*>/i, (match) => `${match}${meta}`);
  }
  if (/<html[^>]*>/i.test(content)) {
    return content.replace(/<html[^>]*>/i, (match) => `${match}<head>${meta}</head>`);
  }
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${meta}<style>body{font-family:system-ui,sans-serif;margin:16px}</style></head><body>${content}</body></html>`;
}

export function previewDocument(artifact: ChatArtifact): string {
  if (artifact.kind === "svg") {
    return injectHead(
      `<style>html,body{margin:0;height:100%;display:flex;align-items:center;justify-content:center;background:#fff}svg{max-width:100%;max-height:100%}</style>${artifact.content}`,
    );
  }
  return injectHead(artifact.content);
}

export function artifactFileName(artifact: ChatArtifact): string {
  const ext =
    artifact.kind === "html"
      ? "html"
      : artifact.kind === "svg"
        ? "svg"
        : artifact.kind === "markdown"
          ? "md"
          : extForLang(artifact.lang);
  const base =
    artifact.title
      .replace(/\.[a-z0-9]{1,5}$/i, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "artifact";
  return `${base}.${ext}`;
}
