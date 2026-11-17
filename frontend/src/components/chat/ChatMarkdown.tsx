/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

import { Children, createContext, memo, useContext, useMemo, useState, useEffect, useRef } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { Icon } from "@filigran/design-system";
// The @filigran/chatbot panel's normalisation and URL allow-list, so this
// renderer and the panel read assistant prose identically.
//
// Import from the `/markdown` subpath, NEVER the package root: the root entry
// is the ~90 kB panel bundle, and its stylesheet redefines Tailwind utilities
// this app also uses. The subpath is ~2 kB of pure string helpers with no
// React import.
import { markdownUrlTransform, parseInlineMarkerHref, prepareMarkdown } from "@filigran/chatbot/markdown";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { cn, saveBlobAs } from "@/lib/utils";
import { api, getToken } from "@/lib/api";
import { useToast } from "@/components/ui/toast";
import { Button } from "@/components/ui/button";
import { withBasePath } from "@/lib/base-path";
import { downloadErrorDescription } from "@/lib/download-error";
// Both are plain, React-light modules (a context object; string parsing) —
// neither pulls the deck runtime into this eagerly loaded chunk.
import { PresentationCardContext } from "@/components/presentation/presentation-card-context";
import { InlineMarkerContext } from "@/components/chat/inline-marker-context";
import { parsePresentationLink } from "@/components/presentation/slide-links";
import { LocalPathInline } from "@/components/desktop/LocalPath";
import { DESKTOP_FEATURE_LOCAL_PATHS, desktopSupports, getDesktopBridge } from "@/lib/desktop";
import { fileUrlToLocalPath, looksLikeLocalPath } from "@/lib/local-paths";
import type { DesktopBridge } from "@/types";
import { CHART_FENCE_LANGUAGE } from "@/components/chat/chat-chart";
import { chartCodeBlockRenderer } from "@/components/chat-panel/chart-code-block";
import { chatFileIdFromHref, prepareChatImages } from "@/components/chat/file-image-src";
import { ImageLightbox } from "@/components/chat/ImageLightbox";

/**
 * The Desktop bridge when this page runs in XTM One Desktop AND its shell can
 * open / show local paths; `null` on the web and on an older shell.
 */
function localPathsBridge(): DesktopBridge | null {
  const bridge = getDesktopBridge();
  return bridge && desktopSupports(DESKTOP_FEATURE_LOCAL_PATHS, bridge) ? bridge : null;
}

/**
 * The shared allow-list (http, https, mailto, tel, inline images), plus a
 * `file:` LINK inside XTM One Desktop - which never reaches the DOM as an
 * href: `MarkdownLink` renders it as a local-path button the shell validates.
 */
function chatUrlTransform(url: string, key: string): string {
  if (key === "href" && /^file:/i.test(url) && localPathsBridge()) return url;
  return markdownUrlTransform(url);
}

/* ---------- Chart image (from code interpreter) ---------- */

function ChartImage({ src, alt }: { src: string; alt: string }) {
  const { t_i18n } = useFormatter();
  const [expanded, setExpanded] = useState(false);
  // The model's alt text when it wrote one, else a name in the reader's
  // language: it also names the lightbox dialog.
  const shownAlt = alt.trim() || t_i18n("Generated chart");

  return (
    <>
      <div className="group/chart relative my-4 inline-block max-w-full cursor-pointer rounded-sm border border-elevation-subtle-soft bg-elevation-default p-1 shadow-xs hover:border-accent/30 hover:shadow-[0_0_15px_color-mix(in_srgb,var(--color-accent)_10%,transparent)] transition-all">
        <img
          src={src}
          alt={shownAlt}
          className="max-w-full rounded-sm"
          loading="lazy"
          onClick={() => setExpanded(true)}
        />
        {/* data-ds-exempt: a control on the image scrim; the design system has no
            on-scrim button, so it keeps the scrim tokens (as ImageLightbox). */}
        <button aria-label={t_i18n("Expand")}
          data-ds-exempt="scrim"
          data-testid="chat-image-expand"
          onClick={() => setExpanded(true)}
          className="absolute top-2 right-2 rounded-sm bg-scrim p-1.5 text-on-scrim opacity-0 group-hover/chart:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-on-scrim transition-opacity hover:bg-scrim-strong"
        >
          <Icon name="maximize-2" size={16} />
        </button>
      </div>
      {expanded && (
        <ImageLightbox
          src={src}
          alt={shownAlt}
          onClose={() => setExpanded(false)}
        />
      )}
    </>
  );
}

/* ---------- Authenticated image (for API-served images) ---------- */

/**
 * Fetches an image from an authenticated API endpoint using the JWT token
 * and renders it from a blob URL. Browser `<img>` tags don't send the
 * Authorization header, so API-served images (generated images, file
 * attachments) would fail with 401 without this component.
 */
function AuthenticatedImage({ src, alt }: { src: string; alt: string }) {
  const { t_i18n } = useFormatter();
  const token = getToken();
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [errored, setErrored] = useState(!token);
  const [expanded, setExpanded] = useState(false);
  const blobUrlRef = useRef<string | null>(null);

  useEffect(() => {
    if (!token) return;

    let cancelled = false;
    // Abort the download when the image unmounts (a conversation switched
    // away from must not keep pulling its pictures while the next one
    // loads); the abort rejects the fetch, `cancelled` makes it a
    // no-op instead of an error state.
    const controller = new AbortController();

    // ``src`` is the root-relative platform path (as the tools write it); the
    // URL path prefix the platform is served under goes on here.
    fetch(withBasePath(src), { headers: { Authorization: `Bearer ${token}` }, signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.blob();
      })
      .then((blob) => {
        if (!cancelled) {
          const url = URL.createObjectURL(blob);
          blobUrlRef.current = url;
          setBlobUrl(url);
        }
      })
      .catch(() => { if (!cancelled) setErrored(true); });

    return () => {
      cancelled = true;
      controller.abort();
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
    };
  }, [src, token]);

  if (errored) {
    // A file the reader cannot load (not theirs, gone) names its alt text,
    // never a broken-image icon.
    return <ImageFallback alt={alt} />;
  }

  if (!blobUrl) {
    return (
      <div className="my-4 inline-flex items-center gap-2 rounded-sm border border-elevation-subtle-soft bg-elevation-hover px-4 py-3">
        <div className="h-4 w-4 rounded-full border-2 border-accent/40 border-t-transparent animate-spin" />
        <span className="content-compact text-default-secondary">{t_i18n("Loading image…")}</span>
      </div>
    );
  }

  // The model's alt text when it wrote one, else a name in the reader's
  // language, never an image a screen reader cannot name.
  const shownAlt = alt.trim() || t_i18n("Generated image");
  return (
    <>
      <div className="group/chart relative my-4 inline-block max-w-full cursor-pointer rounded-sm border border-elevation-subtle-soft bg-elevation-hover p-1 shadow-xs hover:border-accent/30 hover:shadow-[0_0_15px_color-mix(in_srgb,var(--color-accent)_10%,transparent)] transition-all">
        <img
          src={blobUrl}
          alt={shownAlt}
          className="max-w-full max-h-[400px] rounded-sm object-contain"
          loading="lazy"
          onClick={() => setExpanded(true)}
        />
        {/* data-ds-exempt: a control on the image scrim; the design system has no
            on-scrim button, so it keeps the scrim tokens (as ImageLightbox). */}
        <button aria-label={t_i18n("Expand")}
          data-ds-exempt="scrim"
          data-testid="chat-image-expand"
          onClick={() => setExpanded(true)}
          className="absolute top-2 right-2 rounded-sm bg-scrim p-1.5 text-on-scrim opacity-0 group-hover/chart:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-on-scrim transition-opacity hover:bg-scrim-strong"
        >
          <Icon name="maximize-2" size={16} />
        </button>
      </div>
      {expanded && (
        <ImageLightbox src={blobUrl} alt={shownAlt} onClose={() => setExpanded(false)} />
      )}
    </>
  );
}

/**
 * An image the chat cannot load: its alt text in its place, never the
 * browser's broken-image icon.
 */
function ImageFallback({ alt }: { alt: string }) {
  const { t_i18n } = useFormatter();
  return (
    <span className="my-2 block content-base text-default-secondary italic">
      {alt.trim()
        ? t_i18n("[{alt}: image could not be loaded]", { alt: alt.trim() })
        : t_i18n("[Image could not be loaded]")}
    </span>
  );
}

/* ---------- Link to a file of the conversation ---------- */

/**
 * `[text](file:<uuid>)` in an answer, rewritten to the file's download path
 * (file-image-src.ts): a link that downloads the file with the reader's
 * session, the way a `[[FILE:id]]` card does. A plain link would
 * open the address without the session and answer 401.
 */
function ChatFileLink({
  href,
  fileId,
  children,
}: {
  href: string;
  fileId: string;
  children?: React.ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  // A link with no text would render 0 px wide, reachable by keyboard only:
  // it reads "File" instead.
  const hasText = Children.toArray(children).some((child) =>
    typeof child === "string" || typeof child === "number" ? String(child).trim() !== "" : true,
  );
  const onClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    // The file is named by the link's text, never by its raw id.
    const linkText = event.currentTarget.textContent?.trim() ?? "";
    api
      .download(`/chat/files/${fileId}/download`)
      .then(({ blob, filename }) => saveBlobAs(blob, filename ?? (linkText || "download")))
      .catch((err: unknown) => {
        toast({
          title: t_i18n("Download failed"),
          description: downloadErrorDescription(err, linkText, t_i18n),
          variant: "error",
        });
      });
  };
  return (
    <a
      href={withBasePath(href)}
      onClick={onClick}
      // ``wrap-anywhere`` (overflow-wrap) breaks a long unbroken name only when
      // it has to; ``break-all`` split words mid-way ("scan res / ults").
      className="text-accent hover:text-accent/80 underline underline-offset-2 decoration-accent/30 hover:decoration-accent/60 transition-colors wrap-anywhere"
    >
      {hasText ? children : t_i18n("File")}
    </a>
  );
}

/* ---------- Copy button for code blocks ---------- */

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text || "");
      setCopied(true);
      setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
    } catch {
      console.warn("Clipboard write failed");
    }
  };

  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={copied ? "Copied" : "Copy code"}
      onClick={handleCopy}
      className="gap-1.5"
    >
      {copied ? (
        <>
          <Icon name="check" size={16} className="text-feedback-success-primary" />
          <span className="text-feedback-success-primary">Copied</span>
        </>
      ) : (
        <>
          <Icon name="copy" size={16} />
          Copy
        </>
      )}
    </Button>
  );
}

/* ---------- Fenced code block ---------- */

function CodeBlock({
  language,
  children,
}: {
  language?: string;
  children: string;
}) {
  return (
    <div data-testid="chat-code-block" className="group my-4 rounded-sm border border-elevation-subtle-soft bg-elevation-hover overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-elevation-subtle-soft bg-elevation-hover">
        <span className="text-[11px] text-default-secondary font-mono-plex">
          {language || "plaintext"}
        </span>
        <CopyButton text={children} />
      </div>
      <div className="overflow-x-auto p-4">
        <code className="block text-[13px] font-mono-plex leading-[1.7] text-default-secondary whitespace-pre">
          {children}
        </code>
      </div>
    </div>
  );
}

/* ---------- Links ---------- */

/**
 * Every link opens in a new tab — except a slide link for a deck that
 * is already on screen: the studio stage beside the rail, or the inline card
 * in this transcript. That one is followed IN PLACE through
 * `PresentationCardContext.navigateToSlide`, so "change applied, see
 * [slide 4](/presentations/<id>#/<slide>)" moves the deck the user is looking
 * at. A modified click (new tab / new window) keeps the browser's own meaning,
 * and a deck no stage is showing opens the viewer on that slide as before.
 */
function MarkdownLink({
  href,
  children,
}: {
  href?: string;
  children?: React.ReactNode;
}) {
  const { navigateToSlide } = useContext(PresentationCardContext);
  const renderInlineMarker = useContext(InlineMarkerContext);
  // A marker kept in its table cell, list item or quote: its chip, in place.
  const inlineMarker = parseInlineMarkerHref(href);
  if (inlineMarker) return <>{renderInlineMarker?.(inlineMarker.kind, inlineMarker.args) ?? null}</>;
  // A `file:` link only survives the URL transform inside XTM One Desktop:
  // open it on this machine, never as a raw href.
  if (href && /^file:/i.test(href)) {
    const bridge = localPathsBridge();
    const path = bridge ? fileUrlToLocalPath(href, bridge.platform) : null;
    return path ? <LocalPathInline path={path}>{children}</LocalPathInline> : <span>{children}</span>;
  }
  // The URL transform empties a link it refuses (`file:` on the web,
  // `javascript:`): an `href=""` would reopen this page in a new tab.
  if (href === "") return <span>{children}</span>;
  // A file of the conversation (`[text](file:<uuid>)`): downloaded
  // with the reader's session.
  const fileId = chatFileIdFromHref(href);
  if (href && fileId) return <ChatFileLink href={href} fileId={fileId}>{children}</ChatFileLink>;
  const onClick = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (
      !navigateToSlide ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    const link = parsePresentationLink(href);
    if (link && link.slideRef !== null && navigateToSlide(link.presentationId, link.slideRef)) {
      event.preventDefault();
    }
  };
  return (
    <a
      // A root-relative platform link (`/presentations/<id>#/<slide>`, as the
      // tools write it) opens under the URL path prefix the platform is
      // served from; absolute and fragment links are left alone.
      href={href === undefined ? undefined : withBasePath(href)}
      target="_blank"
      rel="noopener noreferrer"
      onClick={onClick}
      className="text-accent hover:text-accent/80 underline underline-offset-2 decoration-accent/30 hover:decoration-accent/60 transition-colors break-all"
    >
      {children}
    </a>
  );
}

/* ---------- Markdown components ---------- */

// True under a `pre`: the `code` inside it is a fenced or indented block,
// whatever its language and however many lines it holds. A one-line fence
// with no language has neither a `language-*` class nor a newline, so the
// tree, not the text, tells it from inline code.
const InsidePreContext = createContext(false);

function MarkdownCode({
  className,
  children,
  ...props
}: {
  className?: string;
  children?: React.ReactNode;
}) {
  const insidePre = useContext(InsidePreContext);
  const match = /language-([\w-]+)/.exec(className || "");
  const codeString = String(children).replace(/\n$/, "");

  // A chart the answer carries: drawn when the block is one the chat
  // draws, shown as the code it is otherwise.
  if (match?.[1] === CHART_FENCE_LANGUAGE) {
    const chart = chartCodeBlockRenderer(codeString);
    if (chart) return chart;
  }

  if (insidePre) {
    return (
      <CodeBlock language={match?.[1]}>{codeString}</CodeBlock>
    );
  }

  // An absolute path on this machine, inside XTM One Desktop: one
  // click opens it (or shows it in its folder).
  const bridge = localPathsBridge();
  if (bridge && looksLikeLocalPath(codeString, bridge.platform)) {
    return <LocalPathInline path={codeString} />;
  }

  return (
    <code
      className="rounded-sm bg-elevation-hover px-1.5 py-0.5 text-[13px] font-mono-plex text-accent/80"
      {...props}
    >
      {children}
    </code>
  );
}

const components: Components = {
  /* --- Code --- */
  pre({ children }: { children?: React.ReactNode }) {
    return <InsidePreContext.Provider value={true}>{children}</InsidePreContext.Provider>;
  },

  code: MarkdownCode,

  /* --- Paragraphs --- */
  p({ children }: { children?: React.ReactNode }) {
    return <p className="mb-3 last:mb-0 leading-[1.75] break-words">{children}</p>;
  },

  /* --- Headings --- */
  h1({ children }: { children?: React.ReactNode }) {
    return (
      <h1 className="text-lg font-bold mb-3 mt-5 first:mt-0 text-default-primary">
        {children}
      </h1>
    );
  },
  h2({ children }: { children?: React.ReactNode }) {
    return (
      <h2 className="text-base font-bold mb-2 mt-4 first:mt-0 text-default-primary">
        {children}
      </h2>
    );
  },
  h3({ children }: { children?: React.ReactNode }) {
    return (
      <h3 className="content-base font-semibold mb-2 mt-3 first:mt-0 text-default-primary">
        {children}
      </h3>
    );
  },

  /* --- Lists --- */
  ul({ children }: { children?: React.ReactNode }) {
    return (
      <ul className="mb-3 ml-1 space-y-1.5 [&>li]:relative [&>li]:pl-5 [&>li]:before:absolute [&>li]:before:left-0 [&>li]:before:top-[0.6em] [&>li]:before:h-1.5 [&>li]:before:w-1.5 [&>li]:before:rounded-full [&>li]:before:bg-accent/40">
        {children}
      </ul>
    );
  },
  ol({ children }: { children?: React.ReactNode }) {
    return (
      <ol className="mb-3 ml-1 list-decimal pl-5 space-y-1.5 marker:text-accent/50 marker:font-mono-plex marker:content-compact">
        {children}
      </ol>
    );
  },
  li({ children }: { children?: React.ReactNode }) {
    return <li className="leading-[1.7] break-words">{children}</li>;
  },

  /* --- Blockquotes --- */
  blockquote({ children }: { children?: React.ReactNode }) {
    return (
      <blockquote className="my-4 rounded-r-sm border-l-2 border-accent/30 bg-accent/[0.03] pl-4 pr-3 py-2 text-default-secondary/90 italic">
        {children}
      </blockquote>
    );
  },

  /* --- Tables --- */
  table({ children }: { children?: React.ReactNode }) {
    return (
      <div className="my-4 overflow-x-auto rounded-sm">
        <table className="w-full content-base">{children}</table>
      </div>
    );
  },
  thead({ children }: { children?: React.ReactNode }) {
    return (
      <thead className="bg-elevation-hover border-b border-elevation-subtle-soft">
        {children}
      </thead>
    );
  },
  th({ children }: { children?: React.ReactNode }) {
    return (
      <th className="px-4 py-2.5 text-left content-compact font-semibold uppercase tracking-wider text-default-secondary">
        {children}
      </th>
    );
  },
  td({ children }: { children?: React.ReactNode }) {
    return (
      <td className="px-4 py-2.5 border-t border-elevation-subtle-soft">{children}</td>
    );
  },
  tr({ children }: { children?: React.ReactNode }) {
    return (
      <tr className="hover:bg-elevation-hover transition-colors">{children}</tr>
    );
  },

  /* --- Links --- */
  a: MarkdownLink,

  /* --- Inline formatting --- */
  strong({ children }: { children?: React.ReactNode }) {
    return <strong className="font-semibold text-default-primary">{children}</strong>;
  },
  em({ children }: { children?: React.ReactNode }) {
    return <em className="italic">{children}</em>;
  },

  /* --- Horizontal rule --- */
  hr() {
    return <hr className="my-5 border-elevation-subtle-soft" />;
  },

  /* --- Images --- */
  img({ src, alt }: { src?: string; alt?: string }) {
    // Code interpreter chart images (base64 data URIs) get special treatment
    if (src?.startsWith("data:image/")) {
      return <ChartImage src={src} alt={alt || ""} />;
    }
    // API-served images (generated images, file attachments) need auth headers.
    // The LLM tool returns relative paths like /api/v1/chat/files/{id}/download,
    // but sometimes the LLM "helpfully" prepends an absolute domain, producing
    // e.g. https://some.host/api/v1/chat/files/{id}/download.  Normalise both
    // cases to a relative path so AuthenticatedImage can fetch with the JWT.
    if (src) {
      const apiPathMatch = src.match(/\/api\/v1\/chat\/(?:files|images)\/[^)\s]+/);
      if (apiPathMatch) {
        // Keyed on the address: the renderers are stable, so without it a new
        // address would inherit the previous one's image or error state.
        return <AuthenticatedImage key={apiPathMatch[0]} src={apiPathMatch[0]} alt={alt || ""} />;
      }
    }
    // An address the URL allow-list dropped (a `file:///` path on this
    // machine) loads nothing: its alt text stands in.
    if (!src) return <ImageFallback alt={alt || ""} />;
    return (
      <img
        src={src}
        alt={alt || ""}
        className="my-4 max-w-full rounded-sm"
        loading="lazy"
      />
    );
  },
};

/**
 * The same renderers, but an image at a remote address is shown as a link
 * to it, never loaded: text written by one person and read by others (a
 * work item's description or result) must not make the reader's browser
 * call an address its author chose. Inline (data:) and API-served images
 * stay images.
 */
const linkRemoteImageComponents: Components = {
  ...components,
  img({ src, alt }: { src?: string; alt?: string }) {
    // An address the URL allow-list dropped loads nothing: its alt text stands in.
    if (!src) return alt ? <span>{alt}</span> : null;
    // http(s) and protocol-relative (//host/...) addresses are remote.
    if (/^(?:https?:)?\/\//i.test(src) && !/\/api\/v1\/chat\/(?:files|images)\//.test(src)) {
      return <MarkdownLink href={src}>{alt || src}</MarkdownLink>;
    }
    const Img = components.img as (props: { src?: string; alt?: string }) => React.ReactNode;
    return Img({ src, alt });
  },
};

/* ---------- Public component ---------- */

const REMARK_PLUGINS = [remarkGfm, remarkBreaks];

const MarkdownBody = memo(function MarkdownBody({
  source,
  remoteImages = true,
}: {
  source: string;
  remoteImages?: boolean;
}) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      components={remoteImages ? components : linkRemoteImageComponents}
      urlTransform={chatUrlTransform}
    >
      {source}
    </ReactMarkdown>
  );
});

// Memoized: a page showing answers re-renders on every poll or streamed update,
// and without memo every message on it would re-run the full remark/rehype
// parse each time.
export const ChatMarkdown = memo(function ChatMarkdown({
  content,
  className,
  remoteImages = true,
}: {
  content: string;
  className?: string;
  /** false: an image at a remote address is rendered as a link, not loaded. */
  remoteImages?: boolean;
}) {
  // API images flattened, then `![alt](file:<uuid>)` pointed at the file's
  // download path with its alt kept (file-image-src.ts, #7539).
  const processed = useMemo(() => prepareMarkdown(prepareChatImages(content)), [content]);
  return (
    <div className={cn("break-words overflow-hidden", className ?? "chat-markdown")}>
      <MarkdownBody source={processed} remoteImages={remoteImages} />
    </div>
  );
});
