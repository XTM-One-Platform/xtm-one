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
 * The packaging slide as native blocks: a `formula` ("A + B + C = the offer")
 * and `config_cards` (the configurations a buyer can pick, each a stack of
 * logo rows with a status chip).
 *
 * Every card is one PowerPoint group (`data-export-group`). The result term's
 * gradient is a native gradient shape in the editable PPTX
 * (`data-export-gradient`, read by the extractor in `presentation_export.py`),
 * its words exported as text, never baked into a picture. A dimmed row's
 * chip is a dashed frame, kept as PowerPoint's dash preset; its logo is
 * inked in the muted colour (`tone: muted`, the logo wall's ink).
 *
 * A card, a term's bar and a chip wear a tone or a template palette colour
 * (`brand_1`..`brand_4`, `chart_1`..`chart_6`: `data-color`, as an
 * architecture node does). A logo with a light variant
 * (`logo_light_file_id`) shows it on a light slide, so a white wordmark
 * never sits on white.
 */
import type { DeckConfigCardsBlock, DeckFormulaBlock, DeckFormulaLogo } from "@/types";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { InlineMarkup } from "../inline-markup";
import { markupToPlainText } from "../markup";
import { useSlideScheme } from "../theme";
import { LogoMark } from "./logos";

/** Canvas px of a logo inside a formula card and a configuration row. */
const FORMULA_LOGO_PX = 40;
const CONFIG_LOGO_PX = 28;

/** The operator drawn in each gap: as written, else `+` then a final `=` (`FormulaBlock.resolved_operators`). */
function formulaOperators(block: Pick<DeckFormulaBlock, "terms" | "operators">): string[] {
  const gaps = Math.max(0, block.terms.length - 1);
  if (block.operators && block.operators.length === gaps) return [...block.operators];
  return gaps === 0 ? [] : [...Array<string>(gaps - 1).fill("+"), "="];
}

/** `data-tone` for a tone, `data-color` for a template palette colour (`brand_*`, `chart_*`). */
function toneOrColorAttrs(value: string | undefined): { "data-tone"?: string; "data-color"?: string } {
  const picked = value ?? "accent";
  return /^(brand|chart)_\d$/.test(picked) ? { "data-color": picked } : { "data-tone": picked };
}

/** The logo a slide of *scheme* shows: the light variant on a light slide when there is one. */
function logoForScheme(
  fileId: string,
  lightFileId: string | undefined | null,
  scheme: string | undefined,
): string {
  return scheme === "light" && lightFileId ? lightFileId : fileId;
}

/** The file a slide of *scheme* shows for one logo of a term's row (a bare id or `{file_id, logo_light_file_id}`). */
function rowLogoForScheme(logo: DeckFormulaLogo, scheme: string | undefined): string {
  return typeof logo === "string" ? logo : logoForScheme(logo.file_id, logo.logo_light_file_id, scheme);
}

/** The glyph of an operator: `x` reads as a multiplication sign, `-` as a minus. */
function operatorGlyph(operator: string): string {
  if (operator === "x") return "\u00d7";
  if (operator === "-") return "\u2212";
  return operator;
}

export function FormulaBlockView({ block }: { block: DeckFormulaBlock }) {
  const operators = formulaOperators(block);
  const scheme = useSlideScheme()?.scheme;
  return (
    <figure className="deck-formula" data-fill={block.fill ? "true" : undefined} {...useBlockEditAttrs()}>
      {/* The figure is the inline-size container; the row inside it is what
          its container query styles (a query never styles its own container). */}
      <div className="deck-formula-row" data-terms={block.terms.length} data-align={block.align ?? "center"}>
      {block.terms.map((term, index) => {
        const result = term.variant === "result";
        const marks = term.logo_file_id
          ? [logoForScheme(term.logo_file_id, term.logo_light_file_id, scheme)]
          : (term.logos ?? []).map((logo) => rowLogoForScheme(logo, scheme));
        // A wordmark names the term: its title is then the logo's alt text, never drawn twice.
        const showTitle = term.show_title !== false || marks.length === 0;
        return [
          <div
            key={`term-${index}`}
            className="deck-formula-term"
            data-variant={result ? "result" : "card"}
            {...toneOrColorAttrs(term.tone)}
            data-export-group="true"
            data-export-gradient={result ? "" : undefined}
          >
            {term.icon ? (
              <span className="deck-formula-icon">
                <DeckIcon name={term.icon} />
              </span>
            ) : null}
            {marks.length ? (
              <span className="deck-formula-logos" data-count={marks.length}>
                {marks.map((fileId, logoIndex) => (
                  <LogoMark
                    key={logoIndex}
                    item={{ file_id: fileId, label: showTitle || marks.length > 1 ? undefined : term.title }}
                    look={{ heightPx: FORMULA_LOGO_PX, balance: true, tone: result ? "mono" : undefined }}
                  />
                ))}
              </span>
            ) : null}
            {showTitle ? (
              <InlineMarkup
                as="h4"
                className="deck-formula-title"
                value={term.title}
                field={`terms.${index}.title`}
                singleLine
              />
            ) : null}
            {term.subtitle ? (
              <InlineMarkup
                as="p"
                className="deck-formula-subtitle"
                value={term.subtitle}
                field={`terms.${index}.subtitle`}
              />
            ) : null}
          </div>,
          index < operators.length ? (
            <span key={`op-${index}`} className="deck-formula-operator" aria-hidden="true">
              {operatorGlyph(operators[index])}
            </span>
          ) : null,
        ];
      })}
      </div>
    </figure>
  );
}

export function ConfigCardsBlockView({ block }: { block: DeckConfigCardsBlock }) {
  const scheme = useSlideScheme()?.scheme;
  return (
    <figure
      className="deck-config-cards"
      data-cards={block.cards.length}
      data-row-style={block.row_style ?? "outlined"}
      data-heading={block.heading ? "true" : undefined}
      data-fill={block.fill ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.heading ? (
        <div className="deck-config-cards-heading">
          <InlineMarkup as="span" value={block.heading} field="heading" singleLine />
          <span className="deck-config-cards-rule" aria-hidden="true" />
        </div>
      ) : null}
      {block.cards.map((card, cardIndex) => (
        <div
          key={cardIndex}
          className="deck-config-card"
          {...toneOrColorAttrs(card.tone)}
          data-export-group="true"
        >
          <div className="deck-config-card-head">
            <InlineMarkup
              as="h4"
              className="deck-config-card-title"
              value={card.title}
              field={`cards.${cardIndex}.title`}
              singleLine
            />
            {card.subtitle ? (
              <InlineMarkup
                as="p"
                className="deck-config-card-subtitle"
                value={card.subtitle}
                field={`cards.${cardIndex}.subtitle`}
              />
            ) : null}
          </div>
          <ul className="deck-config-rows">
            {card.rows.map((row, rowIndex) => {
              const dimmed = row.state === "dimmed";
              const tinted = !dimmed && Boolean(row.tinted);
              const chipColor = toneOrColorAttrs(dimmed ? "muted" : row.chip_tone);
              return (
                <li
                  key={rowIndex}
                  className="deck-config-row"
                  data-state={dimmed ? "dimmed" : "active"}
                  data-tinted={tinted ? "true" : undefined}
                  // The muted ink of `LogoMark` keys on an ancestor's `data-logo-tone`.
                  data-logo-tone={dimmed && row.logo_file_id ? "muted" : undefined}
                  {...(tinted ? chipColor : {})}
                >
                  <span className="deck-config-row-name">
                    {row.logo_file_id ? (
                      <LogoMark
                        item={{
                          file_id: logoForScheme(row.logo_file_id, row.logo_light_file_id, scheme),
                          label: row.label,
                        }}
                        look={{ heightPx: CONFIG_LOGO_PX, balance: true, tone: dimmed ? "muted" : undefined }}
                      />
                    ) : null}
                    {row.label && !row.logo_file_id ? (
                      <InlineMarkup
                        as="span"
                        className="deck-config-row-label"
                        value={row.label}
                        field={`cards.${cardIndex}.rows.${rowIndex}.label`}
                        singleLine
                      />
                    ) : null}
                  </span>
                  {row.chip ? (
                    <span className="deck-config-chip" {...(tinted ? {} : chipColor)}>
                      <InlineMarkup value={row.chip} field={`cards.${cardIndex}.rows.${rowIndex}.chip`} singleLine />
                    </span>
                  ) : null}
                </li>
              );
            })}
          </ul>
          {card.footer ? (
            // A tinted band: the label at the start, the value (when given) at the end.
            <p
              className="deck-config-card-footer"
              data-split={card.footer_value ? "true" : undefined}
              title={markupToPlainText(card.footer)}
            >
              <InlineMarkup
                as="span"
                className="deck-config-card-footer-label"
                value={card.footer}
                field={`cards.${cardIndex}.footer`}
              />
              {card.footer_value ? (
                <InlineMarkup
                  as="span"
                  className="deck-config-card-footer-value"
                  value={card.footer_value}
                  field={`cards.${cardIndex}.footer_value`}
                  singleLine
                />
              ) : null}
            </p>
          ) : null}
        </div>
      ))}
    </figure>
  );
}
