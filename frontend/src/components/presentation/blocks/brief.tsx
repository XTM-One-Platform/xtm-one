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
 * Collateral blocks: badges, key_value, pricing_tiers, personas, banner.
 *
 * The building blocks of a product brief / one-pager — the shapes Filigran's
 * sales collateral repeats on every page (a row of certification pills, a
 * "Deployment / Pricing / Support" fact sheet, three plan columns with the
 * middle one highlighted, the "who it is for" persona cards of a pitch, a
 * gradient call-to-action band with buttons). They work on a 16:9 slide as
 * well as on a portrait page.
 *
 * Styling is plain CSS in `presentation.css`, keyed by `data-*` attributes.
 */
import { useRef } from "react";

import type {
  DeckBadgesBlock,
  DeckBannerAction,
  DeckBannerBlock,
  DeckKeyValueBlock,
  DeckPersona,
  DeckPersonasBlock,
  DeckPricingTier,
  DeckPricingTiersBlock,
} from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup, PlainText } from "../inline-markup";
import { SPREAD_ROW_MAX_EXTRA_PX, measureCardRows, useSpreadCap } from "../spread";
import { alignAttr } from "./helpers";
import { IconText } from "./shared";

export function BadgesBlockView({ block }: { block: DeckBadgesBlock }) {
  return (
    <div
      className="deck-badges"
      data-variant={block.variant ?? "outline"}
      data-size={block.size ?? "md"}
      data-align={alignAttr(block.align)}
      {...useBlockEditAttrs()}
    >
      {block.items.map((item, index) => (
        // A pill is the shared icon + text row, on one line.
        <IconText
          className="deck-badge"
          key={index}
          data-tone={item.tone ?? "accent"}
          icon={item.icon ? <DeckIcon name={item.icon} /> : undefined}
        >
          <InlineMarkup value={item.text} field={`items.${index}.text`} />
        </IconText>
      ))}
    </div>
  );
}

export function KeyValueBlockView({ block }: { block: DeckKeyValueBlock }) {
  // `fill` grows the ROWS, each pair centred between its rules, each row by a
  // readable extra at most, never to the floor: the rows are read at their
  // natural height (`measureCardRows`), not at the height they were stretched to.
  const rootRef = useRef<HTMLDListElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items, measureCardRows);
  return (
    <dl
      ref={rootRef}
      className="deck-kv"
      data-columns={block.columns ?? 1}
      data-density={block.density ?? "normal"}
      data-framed={block.framed ? "true" : undefined}
      data-fill={block.fill ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.items.map((item, index) => (
        <div className="deck-kv-row" key={index} data-tone={item.tone}>
          {/* The label is the shared icon + text row; the row's
              baseline is its text's, which the value lines up on. */}
          <IconText
            as="dt"
            className="deck-kv-label"
            icon={item.icon ? <DeckIcon name={item.icon} /> : undefined}
          >
            <InlineMarkup value={item.label} field={`items.${index}.label`} />
          </IconText>
          <InlineMarkup as="dd" className="deck-kv-value" value={item.value} field={`items.${index}.value`} />
        </div>
      ))}
    </dl>
  );
}

function PricingTierCard({ tier, field }: { tier: DeckPricingTier; field: string }) {
  return (
    <div
      className="deck-tier"
      data-tone={tier.tone ?? "accent"}
      data-highlight={tier.highlight ? "true" : undefined}
    >
      {tier.badge ? <InlineMarkup as="div" className="deck-tier-badge" value={tier.badge} field={`${field}.badge`} /> : null}
      <div className="deck-tier-head">
        {tier.icon ? (
          <span className="deck-tier-icon">
            <DeckIcon name={tier.icon} />
          </span>
        ) : null}
        <InlineMarkup as="div" className="deck-tier-name" value={tier.name} field={`${field}.name`} />
      </div>
      <div className="deck-tier-price">
        <PlainText as="span" className="deck-tier-amount" value={tier.price} field={`${field}.price`} />
        {tier.period ? (
          <InlineMarkup as="span" className="deck-tier-period" value={tier.period} field={`${field}.period`} />
        ) : null}
      </div>
      {tier.description ? (
        <InlineMarkup as="div" className="deck-tier-desc" value={tier.description} field={`${field}.description`} />
      ) : null}
      {tier.features?.length ? (
        <ul className="deck-tier-features">
          {tier.features.map((feature, index) => (
            <li key={index}>
              <DeckIcon name="check" />
              {/* Flex row: the markup needs ONE wrapper or a bold run becomes
                  its own flex item and the line is torn apart. */}
              <InlineMarkup as="span" value={feature} field={`${field}.features.${index}`} />
            </li>
          ))}
        </ul>
      ) : null}
      {tier.cta ? <InlineMarkup as="div" className="deck-tier-cta" value={tier.cta} field={`${field}.cta`} /> : null}
    </div>
  );
}

export function PricingTiersBlockView({ block }: { block: DeckPricingTiersBlock }) {
  return (
    <div
      className="deck-tiers"
      data-columns={block.tiers.length}
      data-density={block.density ?? "normal"}
      {...useBlockEditAttrs()}
    >
      {block.tiers.map((tier, index) => (
        <PricingTierCard tier={tier} field={`tiers.${index}`} key={index} />
      ))}
    </div>
  );
}

function PersonaPoints({
  label,
  labelField,
  items,
  itemsField,
  kind,
}: {
  label: string;
  /** The block-level label field (`pains_label` / `gains_label`) - named on
   *  ONE card only (the first showing these points): the label is shared by
   *  every card, and a target published N times would open N editors. */
  labelField?: string;
  items: string[];
  itemsField: string;
  kind: "pains" | "gains";
}) {
  if (!items.length) return null;
  return (
    <div className="deck-persona-points" data-kind={kind}>
      <InlineMarkup as="div" className="deck-persona-points-label" value={label} field={labelField} />
      <ul>
        {items.map((item, index) => (
          <li key={index}>
            <DeckIcon name={kind === "pains" ? "circle-alert" : "check"} />
            {/* Flex row (bullet + text): one wrapper, or a bold run becomes its
                own flex item and the sentence is torn apart. */}
            <InlineMarkup as="span" value={item} field={`${itemsField}.${index}`} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function PersonaCard({
  persona,
  field,
  painsLabel,
  gainsLabel,
  editsLabels,
}: {
  persona: DeckPersona;
  field: string;
  painsLabel: string;
  gainsLabel: string;
  /** Whether THIS card carries the edit targets of the shared labels. */
  editsLabels: { pains: boolean; gains: boolean };
}) {
  return (
    <div className="deck-persona" data-tone={persona.tone ?? "accent"}>
      <div className="deck-persona-head">
        <span className="deck-persona-icon">
          <DeckIcon name={persona.icon ?? "user-round"} />
        </span>
        <div className="deck-persona-identity">
          <InlineMarkup as="div" className="deck-persona-name" value={persona.name} field={`${field}.name`} />
          {persona.tagline ? (
            <InlineMarkup as="div" className="deck-persona-tagline" value={persona.tagline} field={`${field}.tagline`} />
          ) : null}
        </div>
      </div>
      <PersonaPoints
        label={painsLabel}
        labelField={editsLabels.pains ? "pains_label" : undefined}
        items={persona.pains ?? []}
        itemsField={`${field}.pains`}
        kind="pains"
      />
      <PersonaPoints
        label={gainsLabel}
        labelField={editsLabels.gains ? "gains_label" : undefined}
        items={persona.gains ?? []}
        itemsField={`${field}.gains`}
        kind="gains"
      />
    </div>
  );
}

export function PersonasBlockView({ block }: { block: DeckPersonasBlock }) {
  // `fill` spreads the rows of cards up to a readable gap, never to the floor.
  const rootRef = useRef<HTMLDivElement>(null);
  useSpreadCap(rootRef, block.fill, SPREAD_ROW_MAX_EXTRA_PX, block.items);
  // The two labels are block-level and drawn on every card: the first card
  // that shows the points is the one place they are edited in place.
  const firstWithPains = block.items.findIndex((persona) => persona.pains?.length);
  const firstWithGains = block.items.findIndex((persona) => persona.gains?.length);
  return (
    <div
      ref={rootRef}
      className="deck-personas"
      data-columns={block.columns ?? Math.min(block.items.length, 4)}
      data-density={block.density ?? "normal"}
      data-fill={block.fill ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.items.map((persona, index) => (
        <PersonaCard
          persona={persona}
          field={`items.${index}`}
          painsLabel={block.pains_label ?? "Challenges"}
          gainsLabel={block.gains_label ?? "Outcomes"}
          editsLabels={{ pains: index === firstWithPains, gains: index === firstWithGains }}
          key={index}
        />
      ))}
    </div>
  );
}

/** A banner action: a real link when it has a URL (kept clickable in the PDF),
 *  a button-shaped label otherwise. */
function BannerActionView({ action, field, primary }: { action: DeckBannerAction; field: string; primary: boolean }) {
  const body = (
    <>
      {action.icon ? <DeckIcon name={action.icon} /> : null}
      <InlineMarkup value={action.label} field={`${field}.label`} />
    </>
  );
  const variant = primary ? "primary" : "secondary";
  if (action.url) {
    return (
      <a
        className="deck-banner-action"
        data-variant={variant}
        href={action.url}
        target="_blank"
        rel="noopener noreferrer"
      >
        {body}
      </a>
    );
  }
  return (
    <span className="deck-banner-action" data-variant={variant}>
      {body}
    </span>
  );
}

export function BannerBlockView({ block }: { block: DeckBannerBlock }) {
  const actions = block.actions ?? [];
  // One filled button per band: the one marked `primary`, else the first.
  const primaryIndex = actions.some((action) => action.primary)
    ? actions.findIndex((action) => action.primary)
    : 0;
  return (
    <div
      className="deck-banner"
      data-variant={block.variant ?? "gradient"}
      data-tone={block.tone ?? "accent"}
      data-align={alignAttr(block.align)}
      data-size={block.size ?? "md"}
      {...useBlockEditAttrs()}
    >
      {block.icon ? (
        <span className="deck-banner-icon">
          <DeckIcon name={block.icon} />
        </span>
      ) : null}
      <div className="deck-banner-body">
        {block.eyebrow ? (
          <InlineMarkup as="div" className="deck-banner-eyebrow" value={block.eyebrow} field="eyebrow" />
        ) : null}
        <InlineMarkup as="div" className="deck-banner-title" value={block.title} field="title" />
        {block.subtitle ? (
          <InlineMarkup as="div" className="deck-banner-subtitle" value={block.subtitle} field="subtitle" />
        ) : null}
      </div>
      {actions.length ? (
        <div className="deck-banner-actions">
          {actions.map((action, index) => (
            <BannerActionView action={action} field={`actions.${index}`} primary={index === primaryIndex} key={index} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
