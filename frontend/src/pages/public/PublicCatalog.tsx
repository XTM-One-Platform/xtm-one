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

import {
  ArrowRight, Bot, FileText, GraduationCap, Server, Wrench,
} from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";

import { usePublicCatalogSummary } from "@/hooks/api/usePublicCatalogApi";
import { cn, formatCompactNumber } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { AnimatedNumber } from "@/components/ui/animated-number";
import { CatalogSearch } from "./components";

const CATEGORIES = [
  {
    key: "agents",
    label: "Agents",
    icon: Bot,
    path: "/catalog/agents",
    description: "Pre-configured AI agents with personas, tools, and assignments",
  },

  {
    key: "skills",
    label: "Skills",
    icon: GraduationCap,
    path: "/catalog/skills",
    description: "Reusable instruction sets following the Agent Skills spec",
  },
  {
    key: "prompts",
    label: "Prompts",
    icon: FileText,
    path: "/catalog/prompts",
    description: "Ready-to-use prompt templates for agents and chat",
  },
  {
    key: "mcp_servers",
    label: "MCP Servers",
    icon: Server,
    path: "/catalog/mcp-servers",
    description: "Model Context Protocol tool server configurations",
  },
  {
    key: "custom_tools",
    label: "Custom Tools",
    icon: Wrench,
    path: "/catalog/tools",
    description: "User-defined JavaScript tools with input schemas",
  },
];

export default function PublicCatalog() {
  const { t_i18n } = useFormatter();
  const { data } = usePublicCatalogSummary();
  const [search, setSearch] = useState("");

  const filtered = CATEGORIES.filter(
    (c) =>
      t_i18n(c.label).toLowerCase().includes(search.toLowerCase()) ||
      t_i18n(c.description).toLowerCase().includes(search.toLowerCase()),
  );

  return (
    <div className="space-y-14">
      {/* ── Hero ────────────────────────────────────────────────────── */}
      <div className="relative text-center space-y-6 pt-12 pb-2">
        {/* Title */}
        <h1 className="font-sans-geologica text-5xl sm:text-6xl font-bold tracking-tight leading-[1.1]">
          <span className="text-gradient-accent">AI</span> {t_i18n("Catalog")}
        </h1>

        {/* Subtitle */}
        <p className="mx-auto max-w-xl text-base text-default-secondary leading-relaxed">
          {t_i18n("Discover and import agents, skills, prompts, tools, and more: shared by the community. One-click to add any resource to your account.")}
        </p>

        {/* Animated resource counter */}
        {data && data.total > 0 && (
          <div className="inline-flex items-center gap-2 rounded-sm border border-accent/20 bg-accent/[0.06] px-4 py-1.5">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-60" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-accent" />
            </span>
            <span className="content-base font-medium text-default-primary">
              <AnimatedNumber value={data.total} />
            </span>
            <span className="content-base text-default-secondary">
              {t_i18n("{count, plural, one {resource available} other {resources available}}", { count: data.total })}
            </span>
          </div>
        )}
      </div>

      {/* ── Search ──────────────────────────────────────────────────── */}
      <div className="mx-auto max-w-lg">
        <CatalogSearch value={search} onChange={setSearch} placeholder="Search categories..." />
      </div>

      {/* ── Category cards ──────────────────────────────────────────── */}
      <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3 stagger-children">
        {filtered.map(({ key, label, icon: Icon, path, description }) => {
          const count = data?.counts[key] ?? 0;
          return (
            <Link
              key={key}
              to={path}
              className={cn(
                "group relative overflow-hidden rounded-sm border border-elevation-subtle layer-1 bg-elevation-default p-6 backdrop-blur-sm",
                "transition-all duration-300",
                "hover:border-accent/30 hover:layer-1 bg-elevation-default hover:shadow-[0_0_30px_color-mix(in_srgb,_var(--color-accent)_calc(0.15_*_100%),_transparent)]",
              )}
            >
              {/* Top gradient accent */}
              <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/20 to-transparent opacity-0 transition-opacity duration-300 group-hover:opacity-100" />

              {/* Hover gradient overlay */}
              <div className="pointer-events-none absolute inset-0 rounded-sm bg-gradient-to-br from-accent/[0.04] to-transparent opacity-0 transition-opacity duration-300 group-hover:opacity-100" />

              <div className="relative space-y-4">
                {/* Icon + count row */}
                <div className="flex items-center justify-between">
                  <div className="rounded-sm bg-accent/10 p-3 transition-colors duration-300 group-hover:bg-accent/15">
                    <Icon className="h-5 w-5 text-accent" />
                  </div>
                  {data && (
                    <span className="text-2xl font-sans-geologica font-bold text-default-secondary tabular-nums">
                      {formatCompactNumber(count)}
                    </span>
                  )}
                </div>

                {/* Content */}
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <h3 className="font-sans-geologica text-[15px] font-semibold tracking-tight">{t_i18n(label)}</h3>
                    <ArrowRight className="h-3.5 w-3.5 text-default-disabled -translate-x-1 opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:opacity-100 group-hover:text-accent" />
                  </div>
                  <p className="text-[13px] text-default-secondary/80 leading-relaxed">{t_i18n(description)}</p>
                </div>
              </div>
            </Link>
          );
        })}
      </div>
    </div>
  );
}
