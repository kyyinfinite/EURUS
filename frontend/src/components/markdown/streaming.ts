export function detectUnterminatedFence(text: string): boolean {
  const lines = text.split("\n");
  let open: { char: string; length: number; indent: number } | null = null;
  for (const line of lines) {
    const match = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(line);
    if (!match) continue;
    const indent = match[1].length;
    const fenceChar = match[2][0];
    const fenceLength = match[2].length;
    if (match[3].trim().length > 0 && !match[3].trim().startsWith("`")) {
      if (!open) {
        open = { char: fenceChar, length: fenceLength, indent };
      }
      continue;
    }
    if (open && fenceChar === open.char && fenceLength >= open.length && indent <= open.indent) {
      open = null;
    } else if (!open) {
      open = { char: fenceChar, length: fenceLength, indent };
    }
  }
  return open !== null;
}

export function extractTrailingCode(text: string): string | null {
  if (!detectUnterminatedFence(text)) return null;
  const lines = text.split("\n");
  let openIndex = -1;
  let open: { char: string; length: number; indent: number } | null = null;
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^(\s*)(`{3,}|~{3,})(.*)$/.exec(lines[i]);
    if (!match) continue;
    const indent = match[1].length;
    const fenceChar = match[2][0];
    const fenceLength = match[2].length;
    const hasInfo = match[3].trim().length > 0 && !match[3].trim().startsWith("`");
    if (open && !hasInfo && fenceChar === open.char && fenceLength >= open.length && indent <= open.indent) {
      open = null;
      openIndex = -1;
    } else if (!open) {
      open = { char: fenceChar, length: fenceLength, indent };
      openIndex = i;
    }
  }
  if (openIndex === -1) return null;
  return lines.slice(openIndex + 1).join("\n");
}

export function normalizeLang(raw: string | null | undefined): string {
  if (!raw) return "text";
  const token = raw.trim().split(/\s+/)[0] ?? "";
  const lower = token.toLowerCase();
  const ALIASES: Record<string, string> = {
    js: "javascript",
    mjs: "javascript",
    cjs: "javascript",
    ts: "typescript",
    sh: "bash",
    shell: "bash",
    zsh: "bash",
    console: "bash",
    py: "python",
    python3: "python",
    rb: "ruby",
    golang: "go",
    yml: "yaml",
    md: "markdown",
    "c++": "cpp",
    "c#": "csharp",
    plaintext: "text",
    txt: "text",
    "": "text",
  };
  return ALIASES[lower] ?? lower;
}

export function extForLang(lang: string): string {
  const MAP: Record<string, string> = {
    text: "txt",
    bash: "sh",
    shell: "sh",
    python: "py",
    javascript: "js",
    typescript: "ts",
    tsx: "tsx",
    jsx: "jsx",
    json: "json",
    yaml: "yaml",
    toml: "toml",
    html: "html",
    css: "css",
    sql: "sql",
    markdown: "md",
    diff: "diff",
    rust: "rs",
    go: "go",
    java: "java",
    c: "c",
    cpp: "cpp",
    csharp: "cs",
    ruby: "rb",
    php: "php",
    swift: "swift",
    kotlin: "kt",
    xml: "xml",
    mermaid: "mmd",
  };
  return MAP[lang] ?? "txt";
}

export function blockKey(prefix: string, content: string): string {
  let hash = 0;
  const step = Math.max(1, Math.floor(content.length / 512));
  for (let i = 0; i < content.length; i += step) {
    hash = (hash * 31 + content.charCodeAt(i)) | 0;
  }
  return `${prefix}-${content.length}-${(hash >>> 0).toString(36)}`;
}
