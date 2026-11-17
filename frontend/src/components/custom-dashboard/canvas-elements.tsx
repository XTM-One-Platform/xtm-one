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
 * The elements a dashboard canvas draws besides widgets (`canvas.ts`):
 * rectangles, ellipses, lines with their arrows, icons, images and markdown.
 *
 * Each is drawn in its box, in canvas pixels (the stage scales the whole
 * canvas). The colours are what the person picked (hex, checked by the
 * reader); one left unset follows the theme (the accent for shapes and icons,
 * the text colour for markdown). Files - an uploaded image or SVG icon - are
 * read from the dashboard's own asset route with the viewer's session
 * (`useAuthenticatedSrc`); markdown is drawn without raw HTML and without
 * images, its links opening in a new tab.
 */
import { useContext, type CSSProperties } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Icon } from "@filigran/design-system";

import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useAuthenticatedSrc } from "@/hooks/useAuthenticatedSrc";
import { dashboardAssetUrl, useDashboardAccess } from "@/hooks/api/dashboard-access";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";

import type { CanvasElement, DashStyle, Point } from "./canvas";
import { DashboardLiveContext } from "./live";

const THEME_STROKE = "var(--color-accent)";

/** The dash pattern of a stroke, proportional to its width. */
function dashArray(style: DashStyle, width: number): string | undefined {
  const unit = Math.max(1, width);
  if (style === "dashed") return `${unit * 4} ${unit * 3}`;
  if (style === "dotted") return `${unit} ${unit * 2}`;
  return undefined;
}

/** A file of the dashboard, as a blob URL; `null` while it loads, without a dashboard or without a file. */
function useAssetSrc(fileId: string | undefined): string | null {
  const live = useContext(DashboardLiveContext);
  const access = useDashboardAccess();
  return useAuthenticatedSrc(live && fileId ? dashboardAssetUrl(access, live.dashboardId, fileId) : null);
}

function ShapeView({ element, w, h }: { element: Extract<CanvasElement, { kind: "rectangle" | "ellipse" }>; w: number; h: number }) {
  const inset = element.strokeWidth / 2;
  const common = {
    fill: element.fillColor ?? THEME_STROKE,
    fillOpacity: element.fillOpacity,
    stroke: element.strokeWidth > 0 ? (element.strokeColor ?? THEME_STROKE) : "none",
    strokeWidth: element.strokeWidth,
    strokeOpacity: element.strokeOpacity,
    strokeDasharray: dashArray(element.strokeDashStyle, element.strokeWidth),
  };
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="block overflow-visible" aria-hidden="true">
      {element.kind === "rectangle" ? (
        <rect
          x={inset}
          y={inset}
          width={Math.max(0, w - element.strokeWidth)}
          height={Math.max(0, h - element.strokeWidth)}
          rx={element.rx}
          {...common}
        />
      ) : (
        <ellipse cx={w / 2} cy={h / 2} rx={Math.max(0, w / 2 - inset)} ry={Math.max(0, h / 2 - inset)} {...common} />
      )}
    </svg>
  );
}

function FilePlaceholder({ label }: { label: string }) {
  return (
    <div className="flex h-full w-full items-center justify-center gap-2 rounded-sm border border-dashed border-elevation-subtle p-2 text-default-secondary">
      <Icon name="image" size={16} className="shrink-0" />
      <span className="truncate content-caption">{label}</span>
    </div>
  );
}

function IconView({ element, w, h }: { element: Extract<CanvasElement, { kind: "icon" }>; w: number; h: number }) {
  const { t_i18n } = useFormatter();
  const src = useAssetSrc(element.fileId);
  const size = Math.max(1, Math.min(w, h));
  const style: CSSProperties = { opacity: element.opacity, ...(element.color ? { color: element.color } : {}) };
  if (element.icon) {
    return (
      <div
        className={cn("flex h-full w-full items-center justify-center", !element.color && "text-accent")}
        style={style}
        role={element.alt ? "img" : undefined}
        aria-label={element.alt}
        aria-hidden={element.alt ? undefined : true}
      >
        <DeckIcon name={element.icon} size={size} />
      </div>
    );
  }
  if (!element.fileId) return <FilePlaceholder label={t_i18n("No icon yet")} />;
  return src ? (
    <img src={src} alt={element.alt ?? ""} className="h-full w-full object-contain" style={{ opacity: element.opacity }} draggable={false} />
  ) : null;
}

function ImageView({ element }: { element: Extract<CanvasElement, { kind: "image" }> }) {
  const { t_i18n } = useFormatter();
  const src = useAssetSrc(element.fileId);
  if (!element.fileId) return <FilePlaceholder label={t_i18n("No image yet")} />;
  return src ? (
    <img
      src={src}
      alt={element.alt ?? ""}
      className={cn("h-full w-full", element.fit === "cover" ? "object-cover" : element.fit === "fill" ? "object-fill" : "object-contain")}
      style={{ opacity: element.opacity }}
      draggable={false}
    />
  ) : null;
}

const FONT_SIZE = { small: "content-compact", default: "content-base", large: "text-xl", extraLarge: "text-3xl" } as const;
const FONT_FAMILY = { sans: "font-sans", serif: "font-serif", mono: "font-mono-plex" } as const;
const TEXT_ALIGN = { left: "text-left", center: "text-center", right: "text-right" } as const;

function MarkdownView({ element }: { element: Extract<CanvasElement, { kind: "markdown" }> }) {
  return (
    <div
      className={cn(
        "h-full w-full overflow-hidden p-2 leading-relaxed",
        !element.fontColor && "text-default-primary",
        FONT_SIZE[element.fontSize],
        FONT_FAMILY[element.fontFamily],
        TEXT_ALIGN[element.textAlign],
        "[&_a]:text-accent [&_a]:underline [&_code]:rounded-sm [&_code]:bg-elevation-hover [&_code]:px-1 [&_h1]:text-[1.6em] [&_h1]:font-semibold [&_h2]:text-[1.3em] [&_h2]:font-semibold [&_h3]:font-semibold [&_li]:ml-5 [&_ol]:list-decimal [&_p]:my-1 [&_ul]:list-disc",
      )}
      style={{
        ...(element.fontColor ? { color: element.fontColor } : {}),
        ...(element.backgroundColor ? { backgroundColor: element.backgroundColor } : {}),
      }}
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        disallowedElements={["img"]}
        components={{
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          ),
        }}
      >
        {element.markdown}
      </ReactMarkdown>
    </div>
  );
}

/** A block element in its `w` x `h` box. */
export function CanvasElementView({ element, w, h }: { element: CanvasElement; w: number; h: number }) {
  switch (element.kind) {
    case "rectangle":
    case "ellipse":
      return <ShapeView element={element} w={w} h={h} />;
    case "icon":
      return <IconView element={element} w={w} h={h} />;
    case "image":
      return <ImageView element={element} />;
    case "markdown":
      return <MarkdownView element={element} />;
    default:
      return null;
  }
}

/** Room around a line's box for its stroke and its arrowheads. */
function linePadding(strokeWidth: number): number {
  return Math.max(12, strokeWidth * 5);
}

/**
 * A line from `a` to `b` in canvas pixels, drawn in its own box so it stacks
 * with the blocks in the structure's order. `hit` adds a wide invisible
 * stroke the editor picks it by.
 */
export function CanvasLineView({
  id,
  element,
  a,
  b,
  hit,
}: {
  id: string;
  element: Extract<CanvasElement, { kind: "line" }>;
  a: Point;
  b: Point;
  hit?: { selected: boolean; onPointerDown: (event: React.PointerEvent) => void };
}) {
  const pad = linePadding(element.strokeWidth);
  const left = Math.min(a.x, b.x) - pad;
  const top = Math.min(a.y, b.y) - pad;
  const width = Math.abs(a.x - b.x) + pad * 2;
  const height = Math.abs(a.y - b.y) + pad * 2;
  const stroke = element.strokeColor ?? THEME_STROKE;
  const marker = `canvas-arrow-${id.replace(/[^A-Za-z0-9_-]/g, "_")}`;
  const path = `M ${a.x - left} ${a.y - top} L ${b.x - left} ${b.y - top}`;
  return (
    <svg
      className="pointer-events-none absolute overflow-visible"
      style={{ left, top, width, height }}
      width={width}
      height={height}
      aria-hidden="true"
      data-canvas-line={id}
    >
      <defs>
        <marker id={`${marker}-end`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill={stroke} fillOpacity={element.strokeOpacity} />
        </marker>
      </defs>
      <path
        d={path}
        fill="none"
        stroke={stroke}
        strokeWidth={Math.max(element.strokeWidth, 0.5)}
        strokeOpacity={element.strokeOpacity}
        strokeDasharray={dashArray(element.strokeDashStyle, element.strokeWidth)}
        strokeLinecap="round"
        markerStart={element.fromArrow ? `url(#${marker}-end)` : undefined}
        markerEnd={element.toArrow ? `url(#${marker}-end)` : undefined}
      />
      {hit && (
        <path
          d={path}
          fill="none"
          stroke={hit.selected ? "var(--color-accent)" : "transparent"}
          strokeOpacity={hit.selected ? 0.35 : 0}
          strokeWidth={Math.max(14, element.strokeWidth + 10)}
          className="pointer-events-auto cursor-move"
          onPointerDown={hit.onPointerDown}
          data-canvas-hit={id}
        />
      )}
    </svg>
  );
}
