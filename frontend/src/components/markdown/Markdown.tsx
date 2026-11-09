
import {
  createContext,
  isValidElement,
  memo,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import type { PluggableList } from "unified";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import type { Schema } from "hast-util-sanitize";
import rehypeKatex from "rehype-katex";
import "katex/dist/katex.min.css";
import type { AttachmentRef } from "../../lib/types";
import { sanitizeImageSource } from "../../lib/media";
import { parseMessageContent } from "../../lib/media";
import { CodeBlock } from "./CodeBlock";
import { MermaidBlock } from "./MermaidBlock";
import { extractTrailingCode, normalizeLang } from "./streaming";
import { ImageViewer, MediaBlock } from "../media/MediaBlocks";


interface StreamingState {
  streaming: boolean;
  trailingCode: string | null;
}

const StreamingContext = createContext<StreamingState>({
  streaming: false,
  trailingCode: null,
});


const SANITIZE_SCHEMA: Schema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), "kbd", "mark", "figure", "figcaption"],
  attributes: {
    ...defaultSchema.attributes,
    "*": [...(defaultSchema.attributes?.["*"] ?? []), "ariaLabel", "ariaHidden", "role"],
    code: [
      ...(defaultSchema.attributes?.code ?? []),
      ["className", /^language-/, "math-inline", "math-display"],
    ],
    span: [["className", /^math/, /^katex/, "footnote-ref"]],
    div: [["className", /^math/, /^katex/]],
    a: [
      ...(defaultSchema.attributes?.a ?? []),
      ["id", /^user-content-fn/],
      "target",
      "rel",
    ],
    li: [["id", /^user-content-fn/]],
    sup: [["id", /^user-content-fnref/, /^user-content-fn/]],
    img: [
      ...(defaultSchema.attributes?.img ?? []),
      ["loading", "lazy", "eager"],
      ["src", /^(https?:\/\/|\/|\.{1,2}\/|#)/i],
    ],
  },
};


function PreBlock({ children }: { children?: ReactNode }) {
  const { streaming, trailingCode } = useContext(StreamingContext);
  const child = Array.isArray(children) ? children[0] : children;
  if (!isValidElement(child)) {
    return <pre className="code-block">{children}</pre>;
  }
  const props = child.props as { className?: string; children?: ReactNode };
  const code = typeof props.children === "string" ? props.children : String(props.children ?? "");
  const lang = normalizeLang(/language-([\w#+-]+)/.exec(props.className ?? "")?.[1] ?? "");
  const pending =
    streaming &&
    trailingCode !== null &&
    code.trimEnd() === trailingCode.trimEnd();
  if (lang === "mermaid") {
    return <MermaidBlock code={code} pending={pending} />;
  }
  return <CodeBlock code={code} lang={lang} pending={pending} />;
}

function InlineCode({ children }: { children?: ReactNode }) {
  return <code>{children}</code>;
}

function MarkdownLink({ href, children }: { href?: string; children?: ReactNode }) {
  const external = typeof href === "string" && /^https?:\/\//.test(href);
  return (
    <a
      href={href}
      {...(external ? { target: "_blank", rel: "noreferrer noopener" } : {})}
    >
      {children}
    </a>
  );
}

function MarkdownImage({ alt, src, title }: { alt?: string; src?: string; title?: string }) {
  const safeSource = sanitizeImageSource(src);
  if (!safeSource) return null;
  return <img src={safeSource} alt={alt ?? ""} title={title} loading="lazy" />;
}

function MarkdownTable({ children }: { children?: ReactNode }) {
  return (
    <div className="my-3 overflow-x-auto rounded-xl border border-line">
      <table>{children}</table>
    </div>
  );
}

const MARKDOWN_COMPONENTS: Components = {
  pre: PreBlock,
  code: InlineCode,
  a: MarkdownLink,
  img: MarkdownImage,
  table: MarkdownTable,
};

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS: PluggableList = [[rehypeSanitize, SANITIZE_SCHEMA], rehypeKatex];

const MarkdownBody = memo(function MarkdownBody({ text }: { text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={REHYPE_PLUGINS}
      components={MARKDOWN_COMPONENTS}
    >
      {text}
    </ReactMarkdown>
  );
});


export interface MarkdownProps {
  text: string;
  streaming?: boolean;
  className?: string;
}

export function Markdown({ text, streaming = false, className = "" }: MarkdownProps) {
  const [viewer, setViewer] = useState<AttachmentRef | null>(null);
  const segments = useMemo(() => parseMessageContent(text), [text]);
  const trailingCode = useMemo(
    () => (streaming ? extractTrailingCode(text) : null),
    [text, streaming],
  );
  const contextValue = useMemo<StreamingState>(
    () => ({ streaming, trailingCode }),
    [streaming, trailingCode],
  );

  if (!text.trim()) return null;

  return (
    <StreamingContext.Provider value={contextValue}>
      <div className={`prose-agent ${className}`}>
        {segments.map((segment, index) =>
          segment.type === "text" && segment.text && segment.text.trim() ? (
            <MarkdownBody key={`t${index}`} text={segment.text} />
          ) : segment.attachment ? (
            <MediaBlock
              key={`a${index}`}
              attachment={segment.attachment}
              onOpenImage={setViewer}
            />
          ) : null,
        )}
        {viewer && <ImageViewer attachment={viewer} onClose={() => setViewer(null)} />}
      </div>
    </StreamingContext.Provider>
  );
}
