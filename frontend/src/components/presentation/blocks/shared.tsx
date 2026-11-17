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
 * Components shared by the block and layout renderers.
 *
 * Styling is plain CSS in `presentation.css`, keyed by `data-*` attributes: a
 * typed prop (`tone`, `density`, `size`...) becomes exactly one attribute, and
 * the stylesheet owns what it means. Components never compose class strings
 * from spec values, so a spec cannot reach an arbitrary class.
 */
import { useCallback, useState, type CSSProperties, type ReactNode, type SyntheticEvent } from "react";

import { cn } from "@/lib/utils";
import type { DeckIconName, DeckTone } from "@/types";

import { useDeckAssetSrc } from "../asset-context";
import { useBrandPicture } from "../brand-picture";
import { DeckIcon } from "../DeckIcon";
import { markupToPlainText } from "../markup";

/** A platform image referenced by file id (see `asset-context.tsx`). */
export function SlideImage({
  fileId,
  alt,
  className,
  fit,
  onRatio,
  brand = false,
}: {
  fileId?: string | null;
  alt: string;
  className?: string;
  fit?: "contain" | "cover";
  /**
   * The intrinsic ratio once the bytes arrive (0 for a broken image), for a
   * box drawn AROUND the picture that must take its shape (the showcase frame):
   * the `<img>` publishes it on itself, an ancestor cannot read it.
   */
  onRatio?: (ratio: number) => void;
  /**
   * A picture a BLOCK places (a row logo, an image, a profile line's logo):
   * one of the template's brand assets reads like the chrome reads it, the
   * scheme's variant and the template's mark ink (`brand-picture.ts`). The
   * chrome and the backdrops resolve their own and leave it off.
   */
  brand?: boolean;
}) {
  const picture = useBrandPicture(brand ? fileId : null);
  const src = useDeckAssetSrc(picture?.fileId ?? fileId);
  if (!src) return null;
  // Keyed on the URL so a re-resolved asset measures again from scratch.
  return (
    <MeasuredImage
      key={src}
      src={src}
      alt={markupToPlainText(alt)}
      className={className ?? "deck-image"}
      fit={fit}
      onRatio={onRatio}
      inked={picture?.inked ?? false}
    />
  );
}

/**
 * An `<img>` that publishes its intrinsic aspect ratio as `--deck-image-ratio`.
 *
 * A size bucket (`sm` / `md` / `lg`) is a TARGET the stylesheet fits the image
 * into, not a cap on its pixel count: brand assets are SVGs whose intrinsic size
 * is a few dozen pixels (a 119x26 wordmark, a 24x24 mark), and capping alone
 * left them microscopic on the 1920px canvas. CSS cannot fit a replaced element
 * inside a width x height box while keeping its ratio AND allowing it to grow —
 * the min/max constraint rules keep the bucket height and letterbox — so the
 * ratio is measured once the bytes arrive and the width is derived from it
 * (`presentation.css`, `.deck-image[data-sized]`). Until then the element is
 * `data-deck-pending`, which holds the export's ready signal: the static render
 * must not be captured between the load and the re-layout.
 */
function MeasuredImage({
  src,
  alt,
  className,
  fit,
  onRatio,
  inked = false,
}: {
  src: string;
  alt: string;
  className: string;
  fit?: "contain" | "cover";
  onRatio?: (ratio: number) => void;
  /**
   * Drawn in the template's mark ink: the image's own pixels are pushed out of
   * its box (`object-position`) and its alpha masks a box painted with
   * `--deck-emblem-ink`, so the layout, the measured ratio and the export's
   * picture stay the `<img>` they were (`presentation.css`, "brand ink").
   */
  inked?: boolean;
}) {
  const [ratio, setRatio] = useState<number | null>(null);
  const measure = useCallback(
    (img: HTMLImageElement) => {
      // 0 = "settled, but no usable ratio" (broken image): the pending marker
      // clears and the stylesheet falls back to the plain cap.
      const measured = img.naturalWidth > 0 && img.naturalHeight > 0 ? img.naturalWidth / img.naturalHeight : 0;
      setRatio(measured);
      onRatio?.(measured);
    },
    [onRatio],
  );
  // A cached image can be complete before React attaches `onLoad`.
  const attach = useCallback(
    (img: HTMLImageElement | null) => {
      if (img?.complete && img.naturalWidth > 0) measure(img);
    },
    [measure],
  );
  const onLoad = useCallback(
    (event: SyntheticEvent<HTMLImageElement>) => measure(event.currentTarget),
    [measure],
  );
  // A file that no longer exists draws nothing, as it does on screen where the
  // authenticated fetch fails first: the static export page must not print a
  // broken-image box in its place.
  const [failed, setFailed] = useState(false);
  const onError = useCallback(() => {
    setRatio(0);
    setFailed(true);
    onRatio?.(0);
  }, [onRatio]);
  // The one inline style besides the theme variables: the value is measured
  // per image at runtime, so it cannot be a class (same exception as `themeStyle`).
  const style = (
    ratio || inked
      ? {
          ...(ratio ? { "--deck-image-ratio": String(ratio) } : {}),
          ...(inked ? { "--deck-brand-mask": `url("${src.replace(/["\\\n\r]/g, encodeURIComponent)}")` } : {}),
        }
      : undefined
  ) as CSSProperties | undefined;
  if (failed) return null;
  return (
    <img
      ref={attach}
      className={className}
      src={src}
      alt={alt}
      data-fit={fit}
      data-brand-ink={inked ? "true" : undefined}
      data-sized={ratio ? "true" : undefined}
      // The export capture waits for every `[data-deck-pending]` to clear.
      data-deck-pending={ratio === null ? "true" : undefined}
      style={style}
      onLoad={onLoad}
      onError={onError}
      draggable={false}
    />
  );
}

/**
 * An icon beside a text label: the ONE row every block draws such a
 * pair with - a table cell's icon and text, a check list's item, a badge, an
 * inline callout head, a comparison side's label, a key / value label, a
 * feature's tile and title, a metric's trend arrow and delta, an events
 * timeline's node and body, a profile line's plate and fact. The stylesheet
 * owns the geometry (`presentation.css`, "An icon beside a label"): the icon
 * centred on the label's FIRST line, a gap of half an em, the icon sized by
 * the type and never shrunk. Three class hooks - `deck-icon-text` on the row,
 * `deck-icon-text-icon` on the slot, `deck-icon-text-label` on the label - and
 * nothing else: a block adds its own classes and `data-*` beside them.
 *
 * `icon` is what sits in the slot: a `DeckIcon`, or a bigger anchor (a tile,
 * a node) whose height the block's stylesheet names in `--deck-icon-text-slot`.
 * Without an icon there is no slot and no gap; with `null` children there is
 * no label box, so an icon-only table cell stays centred. A grid that lays the
 * pair out on shared columns (the vertical process flow) puts the slot and
 * label classes on its own cells instead (`flow.tsx`).
 */
export function IconText({
  as: Tag = "span",
  className,
  icon,
  iconClassName,
  iconAttrs,
  labelAs: Label = "span",
  labelClassName,
  children,
  ...attrs
}: {
  as?: keyof React.JSX.IntrinsicElements;
  className?: string;
  icon?: ReactNode;
  iconClassName?: string;
  iconAttrs?: Record<string, string | undefined>;
  /** `div` when the label holds block children (a title over a description). */
  labelAs?: "span" | "div";
  labelClassName?: string;
  children?: ReactNode;
} & Record<`data-${string}`, string | undefined>) {
  const Row = Tag as "span";
  return (
    <Row className={cn("deck-icon-text", className)} {...attrs}>
      {icon ? (
        <span className={cn("deck-icon-text-icon", iconClassName)} {...iconAttrs}>
          {icon}
        </span>
      ) : null}
      {children === null || children === undefined ? null : (
        <Label className={cn("deck-icon-text-label", labelClassName)}>{children}</Label>
      )}
    </Row>
  );
}

/**
 * A row's picture in place of its marker or icon (`logo_file_id` of a `list`
 * or `feature_list` row): an SVG flag, a small logo. The stylesheet sizes it
 * (`.deck-row-logo`: the first line's cap height in a list, two thirds of the
 * tile in a feature list) with its own aspect ratio, and the `IconText` slot
 * centres it on the row's first line. `border` draws a 1 px hairline so a
 * white flag reads on a white card. The wrapper is ONE export picture
 * (`data-export-picture`), so the editable PPTX places the image, hairline
 * included, where the page drew it.
 */
export function RowLogo({ fileId, border }: { fileId: string; border?: boolean }) {
  return (
    <span className="deck-row-logo" data-border={border ? "true" : undefined} data-export-picture="">
      <SlideImage fileId={fileId} alt="" className="deck-row-logo-img" brand />
    </span>
  );
}

/** An icon-in-a-tile, the framework's "icon box" — used by several blocks. */
export function IconTile({
  icon,
  tone,
  size = "md",
}: {
  icon?: DeckIconName;
  tone?: DeckTone;
  size?: "sm" | "md" | "lg";
}) {
  if (!icon) return null;
  return (
    <span className="deck-icon-tile" data-tone={tone} data-size={size}>
      <DeckIcon name={icon} />
    </span>
  );
}
