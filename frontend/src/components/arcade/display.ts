/**
 * Display names and icons of the waiting arcade. Every string is an
 * English i18n id, translated where it is rendered.
 */
import {
  CalendarDays,
  Crosshair,
  Eye,
  Hammer,
  Hourglass,
  Skull,
  type LucideIcon,
} from "lucide-react";
import type { SegmentedFilterOption } from "@/components/ui/segmented-filter";
import type { ArcadePeriod } from "@/types";
import type { AchievementCategory } from "./engine/achievements";
import type { BossKind, PowerUpKind } from "./engine/rules";

export const BOSS_NAMES: Record<BossKind, string> = {
  hallucination: "The Hallucination",
  rateLimiter: "The Rate Limiter",
  promptInjector: "The Prompt Injector",
  tokenOverflow: "The Token Overflow",
};

export const POWER_UP_NAMES: Record<PowerUpKind, string> = {
  rapid: "Rapid fire",
  spread: "Spread shot",
  laser: "Laser",
  shield: "Shield",
  slow: "Token throttle",
  missiles: "Missiles +3",
  sudo: "sudo mode",
  oneUp: "Extra life",
  nuke: "Tactical nuke",
};

export const PERIOD_OPTIONS: SegmentedFilterOption<ArcadePeriod>[] = [
  { key: "day", label: "Last 24 hours" },
  { key: "week", label: "Last 7 days" },
  { key: "all", label: "All time" },
];

export const ACHIEVEMENT_CATEGORY_ICONS: Record<AchievementCategory, LucideIcon> = {
  skill: Crosshair,
  secret: Eye,
  destruction: Hammer,
  waiting: Hourglass,
  boss: Skull,
  calendar: CalendarDays,
};
