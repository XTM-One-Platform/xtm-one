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

import { useNavigate } from "react-router-dom";
import { SlidersHorizontal, Upload, Wand2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { BackButton } from "@/components/ui/back-button";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";

/**
 * Mode selection page at /agents/new — the first step of agent creation.
 *
 * Three ways to build an agent, each a card:
 *  - Wizard: guided step-by-step construction with a canvas mirror, at
 *    /agents/new/wizard;
 *  - Advanced: the full template-select + configuration form, relocated to
 *    /agents/new/advanced;
 *  - Import: from a file, on the dedicated page at /agents/new/import with
 *    the pre-flight check report.
 */
/** The mode cards' copy, read by `t_i18n(mode.title)` / `t_i18n(mode.description)`,
 *  so every value is a catalog key `validate-i18n` checks. @i18n-keys */
const MODE_COPY = {
  wizard: {
    title: "Wizard",
    description: "Build your agent step by step on a guided canvas, with instructions at every stage. The easiest way to get started.",
  },
  advanced: {
    title: "Advanced",
    description: "Pick a template or start from scratch, then configure persona, integrations and assignments yourself. Full control.",
  },
  import: {
    title: "Import",
    description: "Import an agent from a file: XTM One native bundle or Microsoft AgentSchema document (JSON or YAML).",
  },
} as const;

export default function NewAgentMode() {
  const { t_i18n } = useFormatter();
  const navigate = useNavigate();
  const agenticAIEnabled = useAgenticAIEnabled();
  const aiOff = useAiActionDisabled();

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating agents" />;

  const modes: {
    key: string;
    icon: typeof Wand2;
    title: string;
    description: string;
    disabled?: boolean;
    badge?: string;
    onSelect?: () => void;
  }[] = [
    {
      key: "wizard",
      icon: Wand2,
      ...MODE_COPY.wizard,
      onSelect: () => navigate("/agents/new/wizard"),
    },
    {
      key: "advanced",
      icon: SlidersHorizontal,
      ...MODE_COPY.advanced,
      onSelect: () => navigate("/agents/new/advanced"),
    },
    {
      key: "import",
      icon: Upload,
      ...MODE_COPY.import,
      onSelect: () => navigate("/agents/new/import"),
    },
  ];

  return (
    <div className="space-y-6">
      <BackButton to="/agents" label="Back to Agents" />
      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Create New Agent")}</h1>
        <p className="mt-2 text-default-secondary">{t_i18n("Choose how you want to build your agent.")}</p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 max-w-4xl mx-auto">
        {modes.map((mode) => {
          const Icon = mode.icon;
          const disabled = mode.disabled ?? false;
          return (
            <button
              key={mode.key}
              type="button"
              disabled={disabled}
              onClick={mode.onSelect}
              className={cn(
                "rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default text-default-primary p-6 text-left transition-all",
                disabled ? "opacity-60" : "cursor-pointer hover:bg-elevation-hover",
              )}
            >
              <div className="flex items-center gap-3 mb-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-sm bg-accent/10">
                  <Icon className="h-5 w-5 text-accent" />
                </div>
                <p className="content-base font-semibold">{t_i18n(mode.title)}</p>
                {mode.badge && <Badge variant="outline" className="text-[9px] ml-auto">{t_i18n(mode.badge)}</Badge>}
              </div>
              <p className="content-compact text-default-secondary">{t_i18n(mode.description)}</p>
            </button>
          );
        })}
      </div>
    </div>
  );
}
