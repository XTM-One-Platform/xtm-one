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
 * The report renderer for agent-generated executive reports.
 *
 * Renders a validated `ReportSpec` as a document: the masthead, then one
 * component per block in order, then the footer. Nothing here is generated — the
 * agent supplies typed values and this file plus `report.css` own every element,
 * class and rule.
 *
 * Theme resolution mirrors `PresentationDeck`: three built-in themes keyed on
 * `data-report-theme`, optionally overridden by the whitelisted `theme_config`
 * resolved from the report's linked template container. The override is
 * applied by iterating a fixed key → CSS-custom-property map, so a key the
 * backend whitelist ever stopped filtering still could not reach the DOM.
 */
import { useEffect, useState } from "react";

import { isSafeThemeValue, trimThemeValue } from "@/components/presentation/theme-value";
import { useFormatter } from "@/i18n";
import type { Report, ReportBlock, ReportThemeConfig } from "@/types";

import { ReportBlockBody, RichText } from "./blocks";
import "./report.css";

/** Whitelisted brand keys → the CSS custom property each one drives.
 *
 *  The single place a `theme_config` value can reach the DOM. Iterating this map
 *  (rather than spreading the object) is what guarantees an unexpected key is
 *  dropped instead of forwarded, and each value passes the same guard the
 *  backend resolved it through (`isSafeThemeValue`) so no value could
 *  make the reader's browser load a URL even if the server let one by. */
const THEME_VARIABLES: Record<keyof ReportThemeConfig, string> = {
  background: "--report-bg",
  foreground: "--report-fg",
  accent: "--report-accent",
  muted: "--report-muted",
  heading_font: "--report-heading-font",
  body_font: "--report-body-font",
  paper: "--report-paper",
  card: "--report-card",
  line: "--report-line",
  critical: "--report-critical",
  elevated: "--report-elevated",
  moderate: "--report-moderate",
  controlled: "--report-controlled",
};

function themeStyle(config: ReportThemeConfig | undefined) {
  const style: Record<string, string> = {};
  if (!config) return style;
  for (const [key, variable] of Object.entries(THEME_VARIABLES)) {
    const value = config[key as keyof ReportThemeConfig];
    if (value && isSafeThemeValue(key, value)) style[variable] = trimThemeValue(value);
  }
  return style;
}

/**
 * Force every collapsible section open while the browser is printing.
 *
 * A closed `<details>` prints as its summary line alone, so annex content would
 * silently vanish from an exported PDF — the reader gets a document that looks
 * complete and is not. CSS cannot fix this (browsers hide the content through
 * shadow-DOM slotting, not a `display` rule a descendant selector can override),
 * so the `open` prop is driven from state here instead.
 *
 * `beforeprint` / `afterprint` cover the ad-hoc Cmd+P case; the print *route*
 * passes `variant="print"` and starts in the forced-open state, because Safari
 * fires no `beforeprint` at all.
 */
function useForcedOpenOnPrint(initial: boolean) {
  const [forceOpen, setForceOpen] = useState(initial);

  useEffect(() => {
    if (initial) return;
    const open = () => setForceOpen(true);
    const restore = () => setForceOpen(false);
    window.addEventListener("beforeprint", open);
    window.addEventListener("afterprint", restore);
    return () => {
      window.removeEventListener("beforeprint", open);
      window.removeEventListener("afterprint", restore);
    };
  }, [initial]);

  return forceOpen;
}

/** Anchor id for the block at *index* — the one place this is computed. */
function anchorFor(index: number) {
  return `report-block-${index}`;
}

/**
 * The table of contents, DERIVED from the blocks rather than authored.
 *
 * Anything an agent could write here is already in the spec, and a hand-authored
 * TOC is a TOC that goes stale the moment the report is revised — which is a
 * thing reports do, in place, on every turn. So it is computed from the blocks
 * that carry a title, and it simply disappears when there are too few sections
 * to navigate.
 */
function TableOfContents({ blocks }: { blocks: ReportBlock[] }) {
  const { t_i18n } = useFormatter();
  const entries = blocks
    .map((block, index) => ({ block, index }))
    .filter(({ block }) => !!block.title);
  // A two-item contents list is furniture, not navigation.
  if (entries.length < 3) return null;
  return (
    <nav className="report-toc" aria-label={t_i18n("Contents")}>
      {entries.map(({ block, index }) => (
        <a className="report-toc-link" href={`#${anchorFor(index)}`} key={index}>
          {block.number ? (
            <span className="report-toc-number">{block.number}</span>
          ) : null}
          {block.title}
        </a>
      ))}
    </nav>
  );
}

export function ReportView({
  report,
  variant = "embedded",
}: {
  report: Report;
  /** `print` starts every annex expanded — see `useForcedOpenOnPrint`. */
  variant?: "embedded" | "standalone" | "print";
}) {
  const { spec, theme_config: themeConfig } = report;
  const forceOpen = useForcedOpenOnPrint(variant === "print");
  const header = spec.header;
  const blocks = spec.blocks ?? [];

  return (
    <div
      className="report-root"
      data-report-theme={spec.theme ?? "light"}
      style={themeStyle(themeConfig)}
    >
      <header className="report-hero">
        <div className="report-wrap report-hero-grid">
          <div>
            {header?.eyebrow ? (
              <div className="report-eyebrow">{header.eyebrow}</div>
            ) : null}
            <h1>{header?.title ?? spec.title}</h1>
            {header?.subtitle ? (
              <p className="report-hero-subtitle">{header.subtitle}</p>
            ) : null}
            {header?.tags?.length ? (
              <div className="report-tags">
                {header.tags.map((tag, index) => (
                  <span className="report-tag" key={index}>
                    {tag}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
          {header?.verdict ? (
            <aside className="report-verdict">
              {header.verdict.label ? (
                <div className="report-verdict-label">{header.verdict.label}</div>
              ) : null}
              <div className="report-verdict-grade">{header.verdict.grade}</div>
              <RichText html={header.verdict.rich_text} />
            </aside>
          ) : null}
        </div>
      </header>

      <main className="report-main">
        <div className="report-wrap">
          <TableOfContents blocks={blocks} />
          {blocks.map((block, index) => (
            <ReportBlockBody
              // Index-keyed on purpose: blocks carry no stable id, and an
              // in-place edit replaces the whole list at once (the report is
              // re-fetched, never patched), so there is no reorder to preserve.
              key={index}
              block={block}
              forceOpen={forceOpen}
              anchorId={anchorFor(index)}
            />
          ))}
          {spec.footer ? <div className="report-footer">{spec.footer}</div> : null}
        </div>
      </main>
    </div>
  );
}
