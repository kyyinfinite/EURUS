/** Shiki highlighter service — one lazy singleton for the whole workspace.
 *
 *  Shiki (and its grammars) are loaded via dynamic import the first time a
 *  code block actually renders, so the initial paint never pays for it.
 *  Unknown languages degrade gracefully to plain text — never an error.
 */

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
      // Allow a later retry after a failed load (e.g. offline dev server).
      highlighterPromise = null;
    });
  }
  return highlighterPromise;
}

const loaded = new Set<string>();

async function ensureLang(highlighter: Highlighter, lang: string): Promise<boolean> {
  if (loaded.has(lang) || lang === "text" || lang === "ansi") return lang !== "text" || true;
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

/** Highlight to an HTML string; resolves null when the language is unknown
 *  or the highlighter is unavailable — callers fall back to plain text. */
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
