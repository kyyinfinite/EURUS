
import type { Highlighter } from "shiki";

const THEME = "github-light";

const LANGS = [
  "typescript",
  "tsx",
  "javascript",
  "jsx",
  "json",
  "jsonc",
  "python",
  "bash",
  "shellscript",
  "html",
  "css",
  "sql",
  "yaml",
  "toml",
  "ini",
  "markdown",
  "diff",
  "rust",
  "go",
  "java",
  "c",
  "cpp",
  "csharp",
  "ruby",
  "php",
  "xml",
  "dockerfile",
  "graphql",
  "text",
];

let highlighterPromise: Promise<Highlighter> | null = null;

export function getHighlighter(): Promise<Highlighter> {
  if (!highlighterPromise) {
    highlighterPromise = (async () => {
      const { createHighlighter } = await import("shiki");
      return createHighlighter({
        themes: [THEME],
        langs: LANGS,
      });
    })();
    highlighterPromise.catch(() => {
      highlighterPromise = null;
    });
  }
  return highlighterPromise;
}

const loaded = new Set<string>();

async function ensureLang(highlighter: Highlighter, lang: string): Promise<boolean> {
  if (lang === "text" || lang === "ansi") return false;
  if (loaded.has(lang)) return true;
  if (highlighter.getLoadedLanguages().includes(lang as never)) {
    loaded.add(lang);
    return true;
  }
  try {
    await highlighter.loadLanguage(lang as never);
    loaded.add(lang);
    return true;
  } catch {
    return false;
  }
}

export async function highlightCode(code: string, lang: string): Promise<string | null> {
  if (lang === "text" || lang === "ansi" || !lang) return null;
  try {
    const highlighter = await getHighlighter();
    if (!(await ensureLang(highlighter, lang))) return null;
    return highlighter.codeToHtml(code, {
      lang,
      theme: THEME,
    });
  } catch {
    return null;
  }
}

export const SHIKI_THEME = THEME;
