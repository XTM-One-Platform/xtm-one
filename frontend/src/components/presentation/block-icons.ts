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

/** One Lucide icon per block type and per layout, for the editor's palette,
 *  toolbar and inspector. */
import {
  ArrowDownFromLine,
  Blocks,
  Boxes,
  Calculator,
  ChartColumn,
  CircleUserRound,
  Columns2,
  CreditCard,
  FileText,
  Flag,
  GitCompare,
  Grid3x3,
  Heading,
  Heading2,
  Image,
  LayoutDashboard,
  LayoutGrid,
  LayoutPanelTop,
  LayoutTemplate,
  List,
  ListChecks,
  ListOrdered,
  type LucideIcon,
  Megaphone,
  MessageSquare,
  Minus,
  MoveVertical,
  Network,
  Presentation,
  Quote,
  Route,
  Rows3,
  SquareCheckBig,
  Table,
  Tags,
  Gauge,
  ChartScatter,
  Type,
  Users,
  UsersRound,
  Video,
  Vote,
  Workflow,
} from "lucide-react";

import type { DeckBlockType, SlideLayout } from "@/types";

export const BLOCK_ICONS: Record<DeckBlockType, LucideIcon> = {
  text: Type,
  heading: Heading,
  quote: Quote,
  list: List,
  callout: MessageSquare,
  metric: Gauge,
  metrics_grid: LayoutGrid,
  table: Table,
  chart: ChartColumn,
  key_value: Rows3,
  badges: Tags,
  feature_list: ListChecks,
  phase_card: Flag,
  numbered_cards: ListOrdered,
  pricing_tiers: CreditCard,
  personas: Users,
  profiles: CircleUserRound,
  timeline: Route,
  process_flow: Workflow,
  section_header: Heading2,
  banner: Megaphone,
  challenge_answer: ArrowDownFromLine,
  image: Image,
  video: Video,
  logo_grid: Grid3x3,
  logo_wall: LayoutPanelTop,
  formula: Calculator,
  config_cards: LayoutTemplate,
  diagram: Network,
  architecture: Boxes,
  org_chart: UsersRound,
  positioning_map: ChartScatter,
  poll: Vote,
  spacer: MoveVertical,
  divider: Minus,
  columns: Columns2,
  comparison: GitCompare,
};

export const LAYOUT_ICONS: Record<SlideLayout, LucideIcon> = {
  title: Presentation,
  section: Flag,
  agenda: ListOrdered,
  content: FileText,
  roadmap: Route,
  takeaways: SquareCheckBig,
  dashboard: LayoutDashboard,
  closing: Blocks,
};
