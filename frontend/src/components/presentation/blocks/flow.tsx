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

/** Sequence blocks: timeline (phases / events) and process_flow (arrows). */
import type { DeckProcessFlowBlock, DeckTimelineBlock, DeckTimelineItem } from "@/types";

import { useRef } from "react";

import { DeckIcon } from "../DeckIcon";
import { useBlockEditAttrs } from "../edit-context";
import { useFlowTitleFit } from "../flow-fit";
import { InlineMarkup, PlainText } from "../inline-markup";
import { LOGO_HEIGHT_PX } from "../logo-sizing";
import { LogoMark } from "./logos";
import { IconText } from "./shared";

/**
 * A step's logo: the lead investor of a round, the partner of a
 * phase - a `logo_grid` logo (trimmed mark, optical balance) tied to its step.
 * Like an unlabelled grid logo it is decorative: the step's text names it.
 */
function TimelineLogo({ item, fileId }: { item: DeckTimelineItem; fileId: string }) {
  const height = item.logo_height ?? "sm";
  return (
    <div className="deck-timeline-logo" data-framed={item.framed === false ? "false" : "true"} data-logo-height={height}>
      <LogoMark item={{ file_id: fileId }} look={{ heightPx: LOGO_HEIGHT_PX[height], balance: true }} />
    </div>
  );
}

export function TimelineBlockView({ block }: { block: DeckTimelineBlock }) {
  const style = block.style ?? "phases";
  // Phases share their rows (node, label, title, description, logo),
  // so the logos sit on one row under the lowest description; `data-logos`
  // says one is drawn. Events draw each under its description.
  const logos = style === "phases" && block.items.some((item) => item.logo_file_id);
  return (
    <ol
      className="deck-timeline"
      data-style={style}
      data-density={block.density ?? "normal"}
      data-fill={block.fill ? "true" : undefined}
      data-logos={logos ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.items.map((item, index) => {
        const logo = item.logo_file_id ? <TimelineLogo item={item} fileId={item.logo_file_id} /> : null;
        const node = item.icon ? (
          <DeckIcon name={item.icon} />
        ) : (
          <PlainText value={item.marker ?? String(index + 1)} field={`items.${index}.marker`} />
        );
        const body = (
          <>
            {item.label ? (
              <InlineMarkup as="div" className="deck-timeline-label" value={item.label} field={`items.${index}.label`} />
            ) : null}
            <InlineMarkup as="div" className="deck-timeline-title" value={item.title} field={`items.${index}.title`} />
            {item.description ? (
              <InlineMarkup
                as="div"
                className="deck-timeline-desc"
                value={item.description}
                field={`items.${index}.description`}
              />
            ) : null}
            {style === "events" ? logo : null}
          </>
        );
        // An event is the shared icon + text row: the node beside the
        // body is the anchor the body's first line - the label when there is
        // one, else the title - centres on. A phase stacks its node over its
        // body on the shared rows and is no pair.
        if (style === "events") {
          return (
            <IconText
              as="li"
              className="deck-timeline-item"
              key={index}
              data-tone={item.tone}
              icon={node}
              iconClassName="deck-timeline-node"
              labelAs="div"
              labelClassName="deck-timeline-body"
            >
              {body}
            </IconText>
          );
        }
        return (
          <li className="deck-timeline-item" key={index} data-tone={item.tone}>
            <div className="deck-timeline-node">{node}</div>
            <div className="deck-timeline-body">{body}</div>
            {logo}
          </li>
        );
      })}
    </ol>
  );
}

export function ProcessFlowBlockView({ block }: { block: DeckProcessFlowBlock }) {
  const direction = block.direction ?? "horizontal";
  // A vertical flow is one grid of icon / title / description columns shared
  // by every step; the icon column exists only when a step draws one.
  // On that grid the icon and the title are the shared icon + text pair
  // by its slot and label classes on the grid's own cells - the row
  // class would make the card a flex row and undo the shared columns. A
  // horizontal step stacks its icon over its title and is no pair.
  const icons = direction === "vertical" && block.steps.some((step) => step.icon);
  const pair = direction === "vertical";
  // A horizontal flow's titles step down one size when their longest word
  // does not fit a step (`flow-fit.ts`); the step widens to it after that.
  // Its steps are the columns of the equal-columns grid, every one the same
  // width, and each arrow is drawn in the gutter after its step: the
  // span below sits in the step's markup but outside its box.
  const rootRef = useRef<HTMLOListElement>(null);
  useFlowTitleFit(rootRef, block.steps);
  return (
    <ol
      ref={rootRef}
      className="deck-flow"
      data-direction={direction}
      data-loop={block.loop ? "true" : undefined}
      data-icons={icons ? "true" : undefined}
      {...useBlockEditAttrs()}
    >
      {block.steps.map((step, index) => (
        <li className="deck-flow-step" key={index} data-tone={step.tone ?? "accent"}>
          <div className="deck-flow-card">
            {step.icon ? (
              <span className={pair ? "deck-flow-icon deck-icon-text-icon" : "deck-flow-icon"}>
                <DeckIcon name={step.icon} />
              </span>
            ) : null}
            <InlineMarkup
              as="div"
              className={pair ? "deck-flow-title deck-icon-text-label" : "deck-flow-title"}
              value={step.title}
              field={`steps.${index}.title`}
            />
            {step.description ? (
              <InlineMarkup
                as="div"
                // On the shared columns the description's first line centres on the icon too.
                className={pair ? "deck-flow-desc deck-icon-text-label" : "deck-flow-desc"}
                value={step.description}
                field={`steps.${index}.description`}
              />
            ) : null}
          </div>
          {index < block.steps.length - 1 || block.loop ? (
            <span className="deck-flow-arrow" aria-hidden="true">
              <DeckIcon
                name={
                  block.loop && index === block.steps.length - 1
                    ? "rotate-ccw"
                    : direction === "horizontal"
                      ? "arrow-right"
                      : "arrow-down"
                }
              />
            </span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
