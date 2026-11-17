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

import { useState } from "react";
import {
  Plus,
  Search,
  HardDrive,
  Database,
  Zap,
  Globe,
  FileText,
  MessageSquare,
  Shield,
  Radar,
  Crosshair,
  Cloud,
  Terminal,
  Bell,
  Cog,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { SearchInput } from "@/components/ui/search-input";
import { useFormatter } from "@/i18n";
import { MCP_PRESETS, type MCPPreset } from "./presets";

// ---------------------------------------------------------------------------
// Icon resolver for presets — logo takes precedence, then iconName, then Cog
// ---------------------------------------------------------------------------

// Keyed by the `iconName` union itself, so a name added there without an
// icon here is a compile error rather than a silent Cog.
const PRESET_ICON_MAP: Record<NonNullable<MCPPreset["iconName"]>, React.ElementType> = {
  Search,
  HardDrive,
  Database,
  Zap,
  Globe,
  FileText,
  MessageSquare,
  Shield,
  Radar,
  Crosshair,
  Cloud,
  Terminal,
  Bell,
};

function PresetIcon({ preset, className }: { preset: MCPPreset; className?: string }) {
  if (preset.logo) {
    return <img src={preset.logo} alt="" className={className ?? "h-5 w-5 object-contain"} />;
  }
  const Icon = preset.iconName ? PRESET_ICON_MAP[preset.iconName] : Cog;
  return <Icon className={className ?? "h-5 w-5 text-accent"} />;
}

// ---------------------------------------------------------------------------
// Preset Picker Grid — matches the Agent template selection layout
// ---------------------------------------------------------------------------

export function PresetPicker({ onSelectPreset, onSelectCustom }: { onSelectPreset: (preset: MCPPreset) => void; onSelectCustom: () => void }) {
  const { t_i18n } = useFormatter();
  const [search, setSearch] = useState("");

  const filtered = (search.trim()
    ? MCP_PRESETS.filter(
        (p) =>
          p.name.toLowerCase().includes(search.toLowerCase()) ||
          p.description.toLowerCase().includes(search.toLowerCase()),
      )
    : [...MCP_PRESETS]).sort((a, b) => a.name.localeCompare(b.name));

  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <SearchInput placeholder={t_i18n("Search presets...")} value={search} onChange={setSearch} className="flex-1 min-w-[200px] max-w-md" />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {/* Custom Server — dashed card matching "From Scratch" in agents */}
        <Card className="cursor-pointer border-dashed hover:bg-elevation-hover transition-all" onClick={onSelectCustom}>
          <CardContent className="pt-6">
            <div className="flex items-center gap-3 mb-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-elevation-hover">
                <Plus className="h-5 w-5 text-default-secondary" />
              </div>
              <p className="content-base font-semibold">{t_i18n("Custom Server")}</p>
            </div>
            <p className="content-compact text-default-secondary">
              {t_i18n("Configure any stdio, SSE, or Streamable HTTP MCP server manually.")}
            </p>
          </CardContent>
        </Card>

        {/* Preset cards */}
        {filtered.map((preset) => (
          <Card
            key={preset.id}
            className="cursor-pointer hover:bg-elevation-hover transition-all"
            onClick={() => onSelectPreset(preset)}
          >
            <CardContent className="pt-6">
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10">
                  <PresetIcon preset={preset} />
                </div>
                <p className="content-base font-semibold">{preset.name}</p>
              </div>
              <p className="content-compact text-default-secondary line-clamp-2">{t_i18n(preset.description)}</p>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
