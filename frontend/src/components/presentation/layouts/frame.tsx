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

/** Pieces shared by the v2 layouts: the slide header and the footer slot. */
import { type ReactNode, useRef } from "react";

import type { DeckLeafBlock } from "@/types";

import { Block } from "../blocks";
import { useFitToHeight } from "../fit";
import { InlineMarkup } from "../inline-markup";

/** Title + subtitle at the top of a content-type slide. */
export function SlideHeader({
  title,
  subtitle,
  children,
}: {
  title?: string;
  subtitle?: string;
  children?: ReactNode;
}) {
  if (!title && !subtitle && !children) return null;
  return (
    <header className="deck-slide-header">
      {title ? <InlineMarkup as="h2" value={title} field="title" /> : null}
      {subtitle ? <InlineMarkup as="p" className="deck-slide-subtitle" value={subtitle} field="subtitle" /> : null}
      {children}
    </header>
  );
}

/** The optional block pinned to the bottom of a slide (callout, quote...). */
export function FooterSlot({ block }: { block?: DeckLeafBlock }) {
  if (!block) return null;
  return (
    <div className="deck-slide-footer-block">
      <Block block={block} path="footer_block" />
    </div>
  );
}

/**
 * Scrollless body area between header and footer slot.
 *
 * The content sits in a shrink-to-fit pair (`fit.ts`): `.deck-fit` is
 * the room the body has, `.deck-fit-content` is the content at its natural
 * height, scaled down when it does not fit so it never paints under the footer
 * block or the chrome. Content within budget renders untouched.
 */
export function SlideBodyArea({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  useFitToHeight(hostRef, contentRef);
  return (
    <div className={className ?? "deck-slide-body"}>
      <div className="deck-fit" ref={hostRef}>
        <div className="deck-fit-content" ref={contentRef}>
          {children}
        </div>
      </div>
    </div>
  );
}
