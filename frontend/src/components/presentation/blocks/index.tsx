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
 * Block renderer for v2 slides.
 *
 * One `case` per entry in the backend's `BLOCK_TYPES` catalog
 * (`backend/app/services/presentation_spec.py`). The backend test suite greps
 * this switch for every catalog id — a block added on one side only would
 * render as an invisible gap on the slide, which is the trap the tripwire
 * exists for. Keep every `case "<block>":` literal on its own line.
 *
 * Styling is plain CSS (`presentation.css`) keyed by `data-*` attributes; block
 * components never build class strings from spec values.
 *
 * In-place editing: every block is rendered under a `BlockPathContext`
 * naming its spec path (`blocks.2`, `columns.0.1`, `blocks.2.children.1`...),
 * which its root publishes as `data-edit-block` in edit mode
 * (`useBlockEditAttrs`) and its text fields extend into `data-edit-text`. The
 * path comes from whoever renders the list (`BlockList` derives one per
 * index from its own); a caller with no path renders exactly as before.
 */
import { type ReactNode, useRef } from "react";

import type { DeckBlock, DeckVAlign } from "@/types";

import { BlockPathContext, useEditMode } from "../edit-context";
import { childPath } from "../edit-paths";
import { SPREAD_BLOCK_MAX_EXTRA_PX, useSpreadCap } from "../spread";
import { spreadsFree } from "../spread-math";
import { ArchitectureBlockView } from "./Architecture";
import {
  BadgesBlockView,
  BannerBlockView,
  KeyValueBlockView,
  PersonasBlockView,
  PricingTiersBlockView,
} from "./brief";
import { FeatureListBlockView, NumberedCardsBlockView, PhaseCardBlockView } from "./cards";
import { ChallengeAnswerBlockView } from "./challenge";
import { ChartBlockView } from "./chart";
import { ConfigCardsBlockView, FormulaBlockView } from "./formula";
import { CalloutBlockView, ColumnsBlockView, ComparisonBlockView } from "./containers";
import { ProcessFlowBlockView, TimelineBlockView } from "./flow";
import { ListBlockView } from "./list";
import { LogoGridBlockView, LogoWallBlockView } from "./logos";
import { DiagramBlockView, ImageBlockView, VideoBlockView } from "./media";
import { MetricBlockView, MetricsGridBlockView } from "./metrics";
import { OrgChartBlockView } from "./OrgChart";
import { PollBlockView } from "./poll";
import { PositioningMapBlockView } from "./PositioningMap";
import { ProfilesBlockView } from "./profiles";
import { QuoteBlockView } from "./quote";
import { TableBlockView } from "./table";
import {
  DividerBlockView,
  HeadingBlockView,
  SectionHeaderBlockView,
  SpacerBlockView,
  TextBlockView,
} from "./text";

/** Render one block by type. Unknown types render nothing (newer backend). */
export function Block({ block, path }: { block: DeckBlock; path?: string }): ReactNode {
  return <BlockPathContext.Provider value={path ?? null}>{renderBlock(block)}</BlockPathContext.Provider>;
}

function renderBlock(block: DeckBlock): ReactNode {
  switch (block.type) {
    case "text":
      return <TextBlockView block={block} />;
    case "heading":
      return <HeadingBlockView block={block} />;
    case "quote":
      return <QuoteBlockView block={block} />;
    case "list":
      return <ListBlockView block={block} />;
    case "callout":
      return <CalloutBlockView block={block} />;
    case "metric":
      return <MetricBlockView block={block} />;
    case "metrics_grid":
      return <MetricsGridBlockView block={block} />;
    case "table":
      return <TableBlockView block={block} />;
    case "chart":
      return <ChartBlockView block={block} />;
    case "key_value":
      return <KeyValueBlockView block={block} />;
    case "badges":
      return <BadgesBlockView block={block} />;
    case "pricing_tiers":
      return <PricingTiersBlockView block={block} />;
    case "personas":
      return <PersonasBlockView block={block} />;
    case "profiles":
      return <ProfilesBlockView block={block} />;
    case "banner":
      return <BannerBlockView block={block} />;
    case "feature_list":
      return <FeatureListBlockView block={block} />;
    case "phase_card":
      return <PhaseCardBlockView block={block} />;
    case "numbered_cards":
      return <NumberedCardsBlockView block={block} />;
    case "timeline":
      return <TimelineBlockView block={block} />;
    case "process_flow":
      return <ProcessFlowBlockView block={block} />;
    case "comparison":
      return <ComparisonBlockView block={block} />;
    case "section_header":
      return <SectionHeaderBlockView block={block} />;
    case "challenge_answer":
      return <ChallengeAnswerBlockView block={block} />;
    case "image":
      return <ImageBlockView block={block} />;
    case "video":
      return <VideoBlockView block={block} />;
    case "logo_grid":
      return <LogoGridBlockView block={block} />;
    case "logo_wall":
      return <LogoWallBlockView block={block} />;
    case "formula":
      return <FormulaBlockView block={block} />;
    case "config_cards":
      return <ConfigCardsBlockView block={block} />;
    case "diagram":
      return <DiagramBlockView block={block} />;
    case "architecture":
      return <ArchitectureBlockView block={block} />;
    case "org_chart":
      return <OrgChartBlockView block={block} />;
    case "positioning_map":
      return <PositioningMapBlockView block={block} />;
    case "poll":
      return <PollBlockView block={block} />;
    case "columns":
      return <ColumnsBlockView block={block} />;
    case "spacer":
      return <SpacerBlockView block={block} />;
    case "divider":
      return <DividerBlockView block={block} />;
    default:
      return null;
  }
}

/** A vertical run of blocks (a slide body, a column, a callout's children). */
export function BlockList({
  blocks,
  className,
  verticalAlign,
  distribute,
  path,
}: {
  blocks: readonly DeckBlock[] | undefined;
  className?: string;
  /** Where the run sits when it is shorter than the space it fills (single-flow slide body). */
  verticalAlign?: DeckVAlign;
  /** Spread the blocks over the space the run fills, the free height landing
   *  between them (single-flow slide body, or a column's run) - up to a
   *  readable gap per block, never to the floor. Wins over `verticalAlign`. */
  distribute?: boolean;
  /** The spec path of the LIST (`blocks`, `columns.0`, `blocks.2.children`);
   *  each block gets `${path}.${index}`. Omitted = no editing targets. */
  path?: string;
}) {
  const runRef = useRef<HTMLDivElement>(null);
  // A run of cards, timelines or grids asked to be distributed reaches the
  // body's floor: no readable-gap cap on it (`spreadsFree`).
  const free = !!distribute && spreadsFree(blocks);
  useSpreadCap(runRef, distribute && !free, SPREAD_BLOCK_MAX_EXTRA_PX, blocks);
  const editing = useEditMode() !== null;
  if (!blocks?.length) return null;
  return (
    <div
      ref={runRef}
      className={className ?? "deck-blocks"}
      data-valign={verticalAlign}
      data-distribute={distribute ? "true" : undefined}
      data-spread-free={free ? "true" : undefined}
      // The list a new block is inserted into, for the editor's palette.
      data-edit-list={editing ? path : undefined}
    >
      {blocks.map((block, index) => (
        <Block key={index} block={block} path={path === undefined ? undefined : childPath(path, index)} />
      ))}
    </div>
  );
}
