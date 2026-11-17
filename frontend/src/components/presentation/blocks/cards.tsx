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

/** Card blocks: feature_list (icon boxes), phase_card, numbered_cards. */
import { useRef } from "react";

import type {
  DeckFeatureListBlock,
  DeckNumberedCard,
  DeckNumberedCardsBlock,
  DeckPhase,
  DeckPhaseCardBlock,
  DeckTone,
} from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { SPREAD_CARD_MAX_EXTRA_PX, SPREAD_ROW_MAX_EXTRA_PX, measureCardRows, useSpreadCap } from "../spread";
import { IconText, IconTile, RowLogo } from "./shared";

export function FeatureListBlockView({ block }: { block: DeckFeatureListBlock }) {
  // `fill` spreads the rows up to a readable gap, never to the floor.
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items);
  return (
    <div
      ref={rootRef}
      className="deck-features"
      data-columns={block.columns ?? 1}
      data-size={block.size ?? "md"}
      data-fill={block.fill ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.items.map((item, index) => (
        // A feature is the shared icon + text row: the tile is the
        // anchor the title's first line centres on, the description under it.
        <IconText
          as="div"
          className="deck-feature"
          key={index}
          data-tone={item.tone ?? "accent"}
          data-logo={item.logo_file_id ? "true" : undefined}
          // A row's picture (`logo_file_id`) takes the tile's place, in the
          // tile's box: the title's first line centres on it the same way.
          icon={
            item.logo_file_id ? (
              <RowLogo fileId={item.logo_file_id} border={item.logo_border} />
            ) : item.icon ? (
              <IconTile icon={item.icon} tone={item.tone ?? "accent"} size={block.size ?? "md"} />
            ) : undefined
          }
          labelAs="div"
          labelClassName="deck-feature-body"
        >
          <InlineMarkup as="div" className="deck-feature-title" value={item.title} field={`items.${index}.title`} />
          {item.description ? (
            <InlineMarkup
              as="div"
              className="deck-feature-desc"
              value={item.description}
              field={`items.${index}.description`}
            />
          ) : null}
        </IconText>
      ))}
    </div>
  );
}

/**
 * One phase card — the `phase_card` block and each column of a roadmap slide.
 * `field` is where the phase lives in the spec relative to the enclosing
 * scope: nothing for the block (its fields are its own), `phases.N` on a
 * roadmap slide.
 */
export function PhaseCard({
  phase,
  fill,
  field,
  editAttrs,
}: {
  phase: DeckPhase;
  fill?: boolean;
  field?: string;
  editAttrs?: Record<string, string | undefined>;
}) {
  const at = (name: string) => (field ? `${field}.${name}` : name);
  return (
    <div
      className="deck-phase"
      data-tone={phase.tone ?? "accent"}
      data-fill={fill ? "true" : undefined}
      {...editAttrs}
    >
      <div className="deck-phase-head">
        {phase.icon ? (
          <span className="deck-phase-icon">
            <DeckIcon name={phase.icon} />
          </span>
        ) : null}
        <div>
          <InlineMarkup as="div" className="deck-phase-title" value={phase.title} field={at("title")} />
          {phase.subtitle ? (
            <InlineMarkup as="div" className="deck-phase-subtitle" value={phase.subtitle} field={at("subtitle")} />
          ) : null}
        </div>
      </div>
      {phase.items?.length ? (
        <ul className="deck-phase-items">
          {phase.items.map((item, index) => (
            <li key={index}>
              {/* The li is a flex row (bullet + text): one wrapper, or every
                  bold / italic run becomes its own flex item and the sentence
                  is torn apart. */}
              <InlineMarkup as="span" value={item} field={at(`items.${index}`)} />
            </li>
          ))}
        </ul>
      ) : null}
      {phase.goal ? (
        <div className="deck-phase-goal">
          <InlineMarkup as="span" className="deck-phase-goal-label" value={phase.goal.label} field={at("goal.label")} />
          <InlineMarkup as="span" className="deck-phase-goal-value" value={phase.goal.value} field={at("goal.value")} />
        </div>
      ) : null}
    </div>
  );
}

export function PhaseCardBlockView({ block }: { block: DeckPhaseCardBlock }) {
  return <PhaseCard phase={block} fill={block.fill} editAttrs={useBlockEditAttrs()} />;
}

/**
 * Numbered cards — the `numbered_cards` block and the takeaways slide body.
 * `field` is the item list's path relative to the enclosing scope (`items` in
 * both cases; the block's scope is the block, the slide's is the slide).
 */
export function NumberedCards({
  items,
  columns,
  tone = "accent",
  fill,
  field = "items",
  editAttrs,
}: {
  items: DeckNumberedCard[];
  columns?: 1 | 2 | 3;
  tone?: DeckTone;
  /** Grow the cards to share the height (the takeaways slide; a `numbered_cards`
   *  block with `fill`) instead of packing them at the top - each row
   *  by a readable extra at most. */
  fill?: boolean;
  field?: string;
  editAttrs?: Record<string, string | undefined>;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, fill, SPREAD_CARD_MAX_EXTRA_PX, items, measureCardRows);
  const cols = columns ?? (items.length <= 3 ? items.length : items.length === 4 ? 2 : 3);
  return (
    <div
      ref={rootRef}
      className="deck-cards"
      data-columns={cols}
      data-tone={tone}
      data-fill={fill ? "true" : undefined}
      {...editAttrs}
    >
      {items.map((item, index) => (
        <div className="deck-card" key={index}>
          <div className="deck-card-number">
            {item.icon ? (
              <DeckIcon name={item.icon} />
            ) : (
              String(item.number ?? index + 1).padStart(2, "0")
            )}
          </div>
          <InlineMarkup as="div" className="deck-card-title" value={item.title} field={`${field}.${index}.title`} />
          {item.description ? (
            <InlineMarkup
              as="div"
              className="deck-card-desc"
              value={item.description}
              field={`${field}.${index}.description`}
            />
          ) : null}
        </div>
      ))}
    </div>
  );
}

export function NumberedCardsBlockView({ block }: { block: DeckNumberedCardsBlock }) {
  return (
    <NumberedCards
      items={block.items}
      columns={block.columns}
      tone={block.tone}
      fill={block.fill}
      editAttrs={useBlockEditAttrs()}
    />
  );
}
