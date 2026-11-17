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

/**
 * Markdown as a text widget writes it: GitHub-flavoured (tables, task
 * lists, strikethrough), the first heading flush with the top. No HTML is
 * run and no image fetched; a link opens in a new tab, and only when it is
 * a web or mail link (`react-markdown`'s own URL check comes first).
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

const SAFE_LINK = /^(https?:|mailto:|\/(?!\/)|#)/i;

export default function MarkdownText({ text, className }: { text: string; className?: string }) {
  return (
    <div
      className={cn(
        "min-w-0 break-words leading-relaxed",
        "[&>*:first-child]:mt-0 [&_a]:text-accent [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:border-elevation-subtle [&_blockquote]:pl-3",
        "[&_code]:rounded-sm [&_code]:bg-elevation-hover [&_code]:px-1 [&_h1]:mt-4 [&_h1]:font-heading [&_h1]:text-2xl [&_h1]:font-semibold",
        "[&_h2]:mt-4 [&_h2]:font-heading [&_h2]:text-xl [&_h2]:font-semibold [&_h3]:mt-3 [&_h3]:font-heading [&_h3]:font-semibold",
        "[&_li]:ml-5 [&_ol]:list-decimal [&_p]:my-2 [&_table]:my-2 [&_td]:border [&_td]:border-elevation-subtle [&_td]:px-2",
        "[&_th]:border [&_th]:border-elevation-subtle [&_th]:px-2 [&_ul]:list-disc",
        className,
      )}
      data-markdown=""
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        disallowedElements={["img"]}
        components={{
          a: ({ href, children }) =>
            href && SAFE_LINK.test(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
