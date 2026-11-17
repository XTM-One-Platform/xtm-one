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
 * An agent's icon in its rounded accent tile: the one drawing of the pattern
 * the home's Agents widget and its Top value drivers card share (#12009, UX
 * design round C2). The icon is the agent's own (`ICON_MAP`), the robot when
 * it has none or an unknown one. `muted` is the tile of a disabled or missing
 * agent; `children` replaces the icon (the Agents widget's running spinner);
 * `fallback` is the glyph of a row with no agent icon (a conversation).
 * Guard: agent-icon-tile.test.ts fails a second hand-written tile on the home.
 */
import type { ElementType, ReactNode } from "react";
import { ICON_MAP } from "@/lib/constants";
import { cn } from "@/lib/utils";

const TILE_SIZES = {
  sm: { tile: "h-6 w-6", icon: "h-3 w-3" },
  digest: { tile: "h-7 w-7", icon: "h-3.5 w-3.5" },
  md: { tile: "h-8 w-8", icon: "h-3.5 w-3.5" },
} as const;

export function AgentIconTile({
  icon,
  size = "md",
  muted = false,
  fallback,
  className,
  children,
}: {
  icon?: string | null;
  fallback?: ElementType;
  size?: keyof typeof TILE_SIZES;
  muted?: boolean;
  className?: string;
  children?: ReactNode;
}) {
  const Icon = ICON_MAP[icon ?? ""] ?? fallback ?? ICON_MAP.Bot;
  const sizes = TILE_SIZES[size];
  return (
    <div
      aria-hidden={children ? undefined : true}
      className={cn(
        "flex shrink-0 items-center justify-center rounded-sm transition-all duration-300",
        sizes.tile,
        muted ? "bg-elevation-hover" : "bg-accent/10 group-hover:bg-accent/15",
        className,
      )}
    >
      {children ?? <Icon className={cn(sizes.icon, "transition-colors", muted ? "text-default-secondary" : "text-accent")} />}
    </div>
  );
}
