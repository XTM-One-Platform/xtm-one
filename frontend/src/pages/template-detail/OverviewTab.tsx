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

import { Clapperboard, LayoutTemplate, Layers, Lock, Moon, PanelBottom, Paperclip, Search, Sun, Tag, Palette, Ratio } from "lucide-react";
import { VideoTemplateRules } from "@/components/video-studio/VideoTemplateRules";
import { rulesFromMetadata } from "@/components/video-studio/video-format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatCard } from "@/components/ui/stat-card";
import { TimestampGrid } from "@/components/ui/timestamp-grid";
import { PaletteStrip } from "@/components/presentation/TemplateGeneratorDialog";
import { templateChromeOf } from "@/components/presentation/chrome-pattern";
import { previewThemeConfigs, THEME_SCHEMES } from "@/components/presentation/previewSpec";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import { templateKindLabel } from "@/lib/constants";
import { cn, pluralize } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import type { TemplateContainerDetail } from "@/types";

/** Render a layout entry's display label. Layout objects are consumer-defined,
 *  so fall back through `id` → `name` → positional index rather than assuming a
 *  shape this UI does not own. Mirrors what the agent tool surfaces. */
function layoutLabel(layout: Record<string, unknown>, index: number): string {
  const id = layout.id ?? layout.name;
  return typeof id === "string" && id ? id : `#${index}`;
}

export function OverviewTab({ template }: { template: TemplateContainerDetail }) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const layouts = template.layouts ?? [];
  const assets = template.assets ?? [];
  // A presentation template's design is its palette PER THEME: the
  // schemes it supports are exactly the ones a deck wearing it may pick.
  const isDeckTemplate = template.kind === "presentation";
  // A video template dresses its videos in a palette per scheme too.
  const isVideoTemplate = template.kind === "video";
  const palettes = isDeckTemplate || isVideoTemplate ? previewThemeConfigs(template) : null;
  const videoRules = isVideoTemplate ? rulesFromMetadata(template.extra_metadata) : null;
  // The footer / header pattern the template declares, read the lenient
  // way the renderer reads it.
  const chrome = isDeckTemplate ? templateChromeOf(template) : null;

  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-3">
        {isVideoTemplate ? (
          <StatCard
            icon={Lock}
            label="Locked choices"
            value={videoRules?.locked.length ?? 0}
            subtitle={t_i18n("Kept on every video")}
          />
        ) : (
          <StatCard
            icon={Layers}
            label="Layouts"
            value={layouts.length}
            subtitle={t_i18n("Defined in this template")}
          />
        )}
        <StatCard
          icon={Paperclip}
          label="Assets"
          value={assets.length}
          subtitle={t_i18n("Fonts, images and logos")}
        />
        <StatCard
          icon={Search}
          label="Lookups"
          value={template.lookup_count}
          subtitle={t_i18n("Times retrieved by an agent")}
        />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="content-base flex items-center gap-2">
            <LayoutTemplate className="h-4 w-4 text-accent" />
            {t_i18n("Overview")}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {template.description && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <div className="content-compact font-medium text-default-secondary">{t_i18n("Description")}</div>
              <p className="content-base text-default-secondary">{template.description}</p>
            </div>
          )}

          {/* The design a deck inherits: one palette per theme the
              template DECLARES it supports. A theme switched off in
              Configuration is not supported — decks wearing this template
              cannot pick it, and the theme picker says so. */}
          {palettes && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <Palette className="h-3 w-3" />{t_i18n("Design")}
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {THEME_SCHEMES.map((scheme) => {
                  const supported = template.supported_schemes.includes(scheme);
                  const Icon = scheme === "dark" ? Moon : Sun;
                  return (
                    <div
                      key={scheme}
                      className={cn(
                        "rounded-sm border p-2",
                        supported ? "border-elevation-subtle-soft" : "border-dashed border-elevation-subtle-soft opacity-60",
                      )}
                    >
                      <div className="flex items-center gap-1.5 content-compact">
                        <Icon className="h-3 w-3 text-default-secondary" />
                        <span className="font-medium">{schemeLabel(scheme)}</span>
                        <Badge variant={supported ? "cyan" : "outline"} className="ml-auto text-[9px] px-1.5 py-0">
                          {supported ? t_i18n("Supported") : t_i18n("Not supported")}
                        </Badge>
                      </div>
                      <div className="mt-2 min-h-4">
                        {supported ? (
                          <PaletteStrip themeConfig={palettes[scheme]} size="sm" />
                        ) : (
                          <p className="content-caption text-default-secondary">
                            {isVideoTemplate
                              ? t_i18n("Videos wearing this template cannot use this theme.")
                              : t_i18n("Decks wearing this template cannot use this theme.")}
                          </p>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              <p className="content-caption text-default-secondary">
                {isVideoTemplate
                  ? t_i18n("Videos inherit these colours, the fonts and the logos; editing the template restyles the next render of every video wearing it.")
                  : t_i18n("Decks inherit these colours, the fonts and the logos live; editing the template restyles every deck wearing it.")}
              </p>
            </div>
          )}

          {/* What every video wearing the template keeps. */}
          {isVideoTemplate && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <Clapperboard className="h-3 w-3" />{t_i18n("Video rules")}
              </div>
              {videoRules ? (
                <VideoTemplateRules rules={videoRules} />
              ) : (
                <p className="content-compact text-default-secondary">
                  {t_i18n("None: videos take this template's palette, fonts and logo, and choose everything else.")}
                </p>
              )}
              <p className="content-caption text-default-secondary">
                {t_i18n("Defaults fill what a video leaves unset; a locked choice always takes the template's value, and the video says so when it saves.")}
              </p>
            </div>
          )}

          {/* The footer / header every deck wearing the template fills. */}
          {isDeckTemplate && chrome && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <PanelBottom className="h-3 w-3" />{t_i18n("Header & footer")}
              </div>
              <dl className="space-y-1 content-compact">
                {chrome.footer && (
                  <div className="flex flex-wrap items-baseline gap-2">
                    <dt className="text-default-secondary">{t_i18n("Footer")}</dt>
                    <dd className="font-mono-plex">{chrome.footer}</dd>
                  </div>
                )}
                {chrome.header && (
                  <div className="flex flex-wrap items-baseline gap-2">
                    <dt className="text-default-secondary">{t_i18n("Header")}</dt>
                    <dd className="font-mono-plex">{chrome.header}</dd>
                  </div>
                )}
              </dl>
              {chrome.variables.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {chrome.variables.map((variable) => (
                    <Badge key={variable.key} variant="outline" className="content-caption gap-1">
                      <span className="font-mono-plex">{`{{${variable.key}}}`}</span>
                      <span className="text-default-secondary">{variable.label}</span>
                      {variable.default && (
                        <span className="text-default-secondary">= {variable.default}</span>
                      )}
                    </Badge>
                  ))}
                </div>
              )}
              <p className="content-caption text-default-secondary">
                {t_i18n("Filled once per deck from its title, its opening slide's subtitle, its event and these fields - in the deck's Header & footer settings, never by editing slides.")}
              </p>
            </div>
          )}

          {/* The attributes agents match on exactly (kind / aspect ratio, and
              the theme as a colour scheme). Grouped together so it is obvious
              these are the retrieval keys, not decoration. */}
          <div className="rounded-sm bg-elevation-hover p-3 space-y-2">
            <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
              <Search className="h-3 w-3" />{t_i18n("Search attributes")}
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant="cyan" className="content-caption">{t_i18n(templateKindLabel(template.kind))}</Badge>
              {template.supported_schemes.map((scheme) => (
                <Badge key={scheme} variant="outline" className="content-caption gap-1">
                  {scheme === "dark" ? <Moon className="h-2.5 w-2.5" /> : <Sun className="h-2.5 w-2.5" />}
                  {schemeLabel(scheme)}
                </Badge>
              ))}
              {template.aspect_ratio && (
                <Badge variant="outline" className="content-caption gap-1">
                  <Ratio className="h-2.5 w-2.5" />{template.aspect_ratio}
                </Badge>
              )}
            </div>
            <p className="content-caption text-default-secondary">
              {isDeckTemplate
                ? t_i18n("Agents match kind and aspect ratio exactly, the theme against the themes this template supports, and the name as a substring.")
                : t_i18n("Agents match kind and aspect ratio exactly, and the name as a substring.")}
            </p>
          </div>

          {template.tags && template.tags.length > 0 && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <Tag className="h-3 w-3" />{t_i18n("Tags")}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {template.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="content-caption">{tag}</Badge>
                ))}
              </div>
            </div>
          )}

          {layouts.length > 0 && (
            <div className="rounded-sm bg-elevation-hover p-3 space-y-1">
              <div className="content-compact font-medium text-default-secondary flex items-center gap-1.5">
                <Layers className="h-3 w-3" />
                {layouts.length} {t_i18n(pluralize(layouts.length, "layout"))}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {layouts.map((layout, index) => (
                  <Badge key={layoutLabel(layout, index)} variant="outline" className="content-caption font-mono-plex">
                    {layoutLabel(layout, index)}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          <TimestampGrid createdAt={template.created_at} updatedAt={template.updated_at} />
        </CardContent>
      </Card>
    </div>
  );
}
