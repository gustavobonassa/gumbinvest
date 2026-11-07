/**
 * Markdown for the AI analyst's answers: paragraphs, headings, lists, tables,
 * code, quotes and links (GitHub-flavoured, via remark-gfm).
 *
 * Safe by construction: react-markdown never renders raw HTML from the text
 * (an answer containing `<script>` shows it as text) and its URL filter drops
 * `javascript:` and other non-web schemes. Two choices on top of that:
 *
 * - Images are shown as links, never loaded. An answer quoting a web page can
 *   carry a remote image, and fetching it on render would tell a third-party
 *   server that this portfolio was just being discussed.
 * - Links open outside the app (in the desktop shell, in the user's browser).
 *
 * Loaded lazily by the chat panel, so the parser only downloads when a
 * conversation is opened — it is not part of the app's first load.
 */
import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  p: ({ node: _node, ...props }) => <p className="my-2 first:mt-0 last:mb-0" {...props} />,
  h1: ({ node: _node, ...props }) => (
    <h3 className="mb-1.5 mt-4 text-[15px] font-semibold text-ink first:mt-0" {...props} />
  ),
  h2: ({ node: _node, ...props }) => (
    <h4 className="mb-1.5 mt-4 text-sm font-semibold text-ink first:mt-0" {...props} />
  ),
  h3: ({ node: _node, ...props }) => (
    <h5 className="mb-1 mt-3 text-sm font-semibold text-ink first:mt-0" {...props} />
  ),
  h4: ({ node: _node, ...props }) => (
    <h6 className="mb-1 mt-3 text-sm font-medium text-ink first:mt-0" {...props} />
  ),
  strong: ({ node: _node, ...props }) => <strong className="font-semibold text-ink" {...props} />,
  ul: ({ node: _node, ...props }) => (
    <ul className="my-2 list-disc space-y-1 pl-5 marker:text-ink-muted" {...props} />
  ),
  ol: ({ node: _node, ...props }) => (
    <ol className="my-2 list-decimal space-y-1 pl-5 marker:text-ink-muted" {...props} />
  ),
  li: ({ node: _node, ...props }) => <li className="pl-0.5 [&>p]:my-0" {...props} />,
  a: ({ node: _node, ...props }) => (
    <a
      {...props}
      target="_blank"
      rel="noopener noreferrer"
      className="break-words text-accent underline-offset-2 hover:underline"
    />
  ),
  img: ({ src, alt }) =>
    src ? (
      <a
        href={src}
        target="_blank"
        rel="noopener noreferrer"
        className="text-accent hover:underline"
      >
        {alt ? `Imagem: ${alt}` : "Imagem"}
      </a>
    ) : null,
  blockquote: ({ node: _node, ...props }) => (
    <blockquote className="my-2 border-l-2 border-line-strong pl-3 text-ink-muted" {...props} />
  ),
  hr: () => <hr className="my-3 border-line" />,
  code: ({ node: _node, className, ...props }) => (
    // Fenced blocks arrive with a language-* class and sit inside <pre>, which
    // supplies the box; inline code gets its own chip.
    <code
      className={
        className
          ? `${className} font-mono text-xs`
          : "rounded bg-surface-hover px-1 py-0.5 font-mono text-[0.85em] text-ink"
      }
      {...props}
    />
  ),
  pre: ({ node: _node, ...props }) => (
    <pre
      className="my-2 overflow-x-auto rounded-lg border border-line bg-surface p-3 [&>code]:bg-transparent [&>code]:p-0"
      {...props}
    />
  ),
  // The panel is narrow: a wide comparison table scrolls inside its own box
  // instead of stretching the bubble past the screen.
  table: ({ node: _node, ...props }) => (
    <div className="my-2 overflow-x-auto rounded-lg border border-line">
      <table className="tnum w-full border-collapse text-xs" {...props} />
    </div>
  ),
  thead: ({ node: _node, ...props }) => <thead className="bg-surface-hover/60" {...props} />,
  th: ({ node: _node, ...props }) => (
    <th
      className="whitespace-nowrap border-b border-line px-2.5 py-1.5 text-left font-semibold text-ink"
      {...props}
    />
  ),
  td: ({ node: _node, ...props }) => (
    <td
      className="border-b border-line px-2.5 py-1.5 align-top [tr:last-child>&]:border-0"
      {...props}
    />
  ),
};

function ChatMarkdown({ text }: { text: string }) {
  return (
    <div className="break-words">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </Markdown>
    </div>
  );
}

// Re-parsing is per message, and only the streaming one changes: earlier
// bubbles keep their text and skip the parse on every token.
export default memo(ChatMarkdown);
