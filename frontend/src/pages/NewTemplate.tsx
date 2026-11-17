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

import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { LayoutTemplate, Building2, Moon, Sparkles, Sun, Eye, RefreshCw, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useAgenticAIEnabled } from "@/hooks/usePlatformConfig";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useCreateTemplateContainer } from "@/hooks/api/useTemplateContainerApi";
import { AgenticGatePage } from "@/components/ui/license-gate-dialog";
import { TemplateGeneratorDialog, PaletteStrip } from "@/components/presentation/TemplateGeneratorDialog";
import { TemplateKindSelect } from "@/components/presentation/TemplateKindSelect";
import {
  candidateToTemplate,
  SUPPORTED_SCHEMES_KEY,
  templateSupportedSchemes,
  THEME_SCHEMES,
} from "@/components/presentation/previewSpec";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import { TemplatePreviewDialog } from "@/components/presentation/TemplatePreviewDialog";
import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import type { PresentationThemeScheme, TemplateCandidate } from "@/types";

export default function NewTemplate() {
  const aiOff = useAiActionDisabled();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const create = useCreateTemplateContainer();
  const { isAdmin } = useAuth();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const agenticAIEnabled = useAgenticAIEnabled();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [kind, setKind] = useState("presentation");
  const [aspectRatio, setAspectRatio] = useState("");
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);
  // The themes a presentation template supports are DECLARED at creation —
  // both by default, so a plain template dresses any deck; the
  // generator proposes what its palettes read as, the designer decides.
  const [schemes, setSchemes] = useState<PresentationThemeScheme[]>([...THEME_SCHEMES]);

  // "Generate with AI": the structured fields the plain form has no
  // editor for (palette, layout catalog, agent guidance) come from the
  // generator and are sent along on create. The form fields stay editable.
  const [generatorOpen, setGeneratorOpen] = useState(false);
  const [generated, setGenerated] = useState<TemplateCandidate | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);

  // `/templates/new?generate=1` (the list page's "Generate with AI" entry)
  // opens the generator straight away, then drops the flag so a refresh does
  // not reopen it over a half-filled form.
  useEffect(() => {
    if (searchParams.get("generate") === "1") {
      setGeneratorOpen(true);
      const next = new URLSearchParams(searchParams);
      next.delete("generate");
      setSearchParams(next, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  if (!agenticAIEnabled || aiOff) return <AgenticGatePage feature="Creating templates" />;

  const isDeckKind = kind.trim().toLowerCase() === "presentation";

  const applyCandidate = (candidate: TemplateCandidate) => {
    setGenerated(candidate);
    setName(candidate.name);
    setDescription(candidate.description ?? "");
    setKind(candidate.kind);
    setAspectRatio(candidate.aspect_ratio ?? "");
    setNewTags(candidate.tags);
    // The generator declares what its palettes read as; the switches below
    // start there and the designer has the last word.
    const proposed = templateSupportedSchemes({ theme_config: candidate.theme_config, extra_metadata: candidate.extra_metadata });
    if (proposed.length > 0) setSchemes(proposed);
  };

  const toggleScheme = (scheme: PresentationThemeScheme, on: boolean) => {
    const next = THEME_SCHEMES.filter((s) => (s === scheme ? on : schemes.includes(s)));
    // A template that supports no theme can dress no deck: the last one stays.
    if (next.length > 0) setSchemes(next);
  };

  // The declaration rides in `extra_metadata` - for a presentation
  // template only; a report has no deck theme and the backend drops the key.
  // The create payload AND the Preview dialog go through this: after Generate
  // with AI the Dark / Light switches edit `schemes`, not the candidate, so a
  // preview built from the candidate alone would still offer the generator's
  // themes while the template about to be created declares the toggled ones.
  const declareSchemes = <T extends Record<string, unknown> | undefined>(extraMetadata: T) =>
    isDeckKind ? { ...(extraMetadata ?? {}), [SUPPORTED_SCHEMES_KEY]: schemes } : extraMetadata;

  const handleCreate = () => {
    const extraMetadata = declareSchemes(generated?.extra_metadata);
    create.mutate(
      {
        name,
        description: description || undefined,
        // `kind` is one of the closed choice the picker offers; the backend
        // refuses anything else with a 422.
        kind: kind.trim().toLowerCase() || undefined,
        // No `theme` label: a template declares the themes it
        // supports (`supported_schemes`), it never carries a style word.
        aspect_ratio: aspectRatio.trim() || undefined,
        tags: newTags.length > 0 ? newTags : undefined,
        theme_config: generated?.theme_config,
        layouts: generated?.layouts,
        extra_metadata: extraMetadata,
        is_company_managed: isCompanyManaged || undefined,
      },
      {
        onSuccess: (created) => {
          toast({ title: t_i18n("Template created"), variant: "success" });
          navigate(`/templates/${created.id}`);
        },
      },
    );
  };

  const generatedLayoutCount = generated?.layouts.length ?? 0;
  const generatedGuidance = typeof generated?.extra_metadata.guidance === "string" ? generated.extra_metadata.guidance : null;

  return (
    <div className="space-y-6">
      <BackButton to="/templates" label="Back to Templates" />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <LayoutTemplate className="h-6 w-6 text-accent" />
          <h1 className="text-2xl font-bold">{t_i18n("New Template")}</h1>
        </div>
        <Button variant="outline" className="gap-1.5" onClick={() => setGeneratorOpen(true)}>
          <Sparkles className="h-4 w-4 text-accent" /> {t_i18n("Generate with AI")}
        </Button>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          {generated ? (
            <div className="space-y-3 rounded-sm border border-accent/20 bg-accent/5 p-4">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-accent" />
                  <div>
                    <p className="content-base font-medium">{t_i18n("Generated configuration")}</p>
                    <p className="content-compact text-default-secondary">
                      {t_i18n(
                        "{count, plural, one {# layout} other {# layouts}}, palette and agent guidance will be saved with the template.",
                        { count: generatedLayoutCount },
                      )}
                    </p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Button variant="outline" size="sm" className="gap-1.5" onClick={() => setPreviewOpen(true)}>
                    <Eye className="h-3.5 w-3.5" /> {t_i18n("Preview")}
                  </Button>
                  <Button variant="outline" size="sm" className="gap-1.5" onClick={() => setGeneratorOpen(true)}>
                    <RefreshCw className="h-3.5 w-3.5" /> {t_i18n("Regenerate")}
                  </Button>
                  <Button variant="ghost" size="sm" className="gap-1.5" onClick={() => setGenerated(null)}>
                    <X className="h-3.5 w-3.5" /> {t_i18n("Clear")}
                  </Button>
                </div>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <PaletteStrip themeConfig={generated.theme_config} />
                {isDeckKind && (
                  <div className="flex items-center gap-1.5">
                    <span className="content-compact text-default-secondary">{t_i18n("Supports")}</span>
                    {schemes.map((scheme) => (
                      <Badge key={scheme} variant="outline" className="text-[9px] px-1.5 py-0 gap-1">
                        {scheme === "dark" ? <Moon className="h-2.5 w-2.5" /> : <Sun className="h-2.5 w-2.5" />}
                        {schemeLabel(scheme)}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
              {generatedGuidance && (
                <p className="line-clamp-3 content-compact leading-relaxed text-default-secondary">
                  <span className="font-medium text-default-primary">{t_i18n("Agent guidance")}: </span>
                  {generatedGuidance}
                </p>
              )}
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-sm bg-accent/5 p-3">
              <p className="content-compact text-accent">
                {t_i18n("Let the assistant draft the palette, typefaces, layout catalog and guidance from a short brief, then adjust anything below.")}
              </p>
              <Button variant="ghost" size="sm" className="gap-1.5 text-accent hover:text-accent/80" onClick={() => setGeneratorOpen(true)}>
                <Sparkles className="h-3.5 w-3.5" /> {t_i18n("Generate with AI")}
              </Button>
            </div>
          )}

          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField id="new-template-name" label={t_i18n("Name")} required helper={t_i18n("The name agents and people pick the template by.")}>
              <Input id="new-template-name" aria-label={t_i18n("Name")} aria-describedby={settingsFieldHintId("new-template-name")} value={name} onChange={(e) => setName(e.target.value)} placeholder={t_i18n("e.g. Quarterly Business Review")} />
            </SettingsField>
            <SettingsField id="new-template-tags" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-template-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-template-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
          </div>

          <SettingsField id="new-template-description" label={t_i18n("Description (optional)")} helper={t_i18n("What the template is for, shown in the list.")}>
            <Input id="new-template-description" aria-label={t_i18n("Description (optional)")} aria-describedby={settingsFieldHintId("new-template-description")} value={description} onChange={(e) => setDescription(e.target.value)} placeholder={t_i18n("What is this template for?")} />
          </SettingsField>

          {/* The attributes agents match on. `kind` is a closed choice:
              Presentation or Report, what the template dresses. The theme is
              not a word typed here: a presentation template DECLARES
              the themes it supports with the switches below. */}
          <div className="grid gap-4 sm:grid-cols-2">
            <SettingsField
              id="new-template-kind"
              label={t_i18n("Kind")}
              required
              helper={t_i18n("What the template dresses: a presentation, a report or a video.")}
              info={t_i18n("Agents look for a template by kind. It can be changed later in Configuration.")}
            >
              <TemplateKindSelect id="new-template-kind" value={kind} onChange={setKind} />
            </SettingsField>
            <SettingsField
              id="new-template-aspect-ratio"
              label={t_i18n("Aspect ratio (optional)")}
              helper={t_i18n("Width to height, for example 16:9 or 4:3.")}
              info={t_i18n("Agents read it when they pick a template for a deck or a video. Empty: no ratio is stated.")}
            >
              <Input id="new-template-aspect-ratio" aria-label={t_i18n("Aspect ratio (optional)")} aria-describedby={settingsFieldHintId("new-template-aspect-ratio")} value={aspectRatio} onChange={(e) => setAspectRatio(e.target.value)} placeholder={t_i18n("e.g. 16:9")} />
            </SettingsField>
          </div>

          {isDeckKind && (
            <div className="space-y-2 rounded-sm bg-elevation-hover p-3">
              <p className="content-compact font-medium">{t_i18n("Themes this template supports")}</p>
              <p className="content-compact text-default-secondary">
                {t_i18n("Decks wearing this template can only pick a supported theme. Each supported theme gets its own palette in Configuration after creation; at least one stays on.")}
              </p>
              <div className="grid gap-2 sm:grid-cols-2">
                {THEME_SCHEMES.map((scheme) => {
                  const on = schemes.includes(scheme);
                  const last = on && schemes.length === 1;
                  const Icon = scheme === "dark" ? Moon : Sun;
                  return (
                    <div
                      key={scheme}
                      className={cn(
                        "flex items-center gap-3 rounded-sm border px-3 py-2",
                        on ? "border-accent/40 bg-accent/5" : "border-dashed border-elevation-subtle-soft",
                      )}
                    >
                      <Icon className={cn("h-4 w-4 shrink-0", on ? "text-accent" : "text-default-secondary")} />
                      <div className="min-w-0 flex-1">
                        <p className="content-compact font-medium">{t_i18n("{scheme} theme", { scheme: schemeLabel(scheme) })}</p>
                        <p className="content-compact text-default-secondary">
                          {on
                            ? last
                              ? t_i18n("Supported - the only theme, so it stays on.")
                              : t_i18n("Supported")
                            : t_i18n("Not supported")}
                        </p>
                      </div>
                      <Switch
                        checked={on}
                        disabled={last}
                        onCheckedChange={(checked) => toggleScheme(scheme, checked)}
                        aria-label={t_i18n("{scheme} theme", { scheme: schemeLabel(scheme) })}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          <div className="rounded-sm bg-accent/5 p-3 space-y-1">
            <p className="content-compact text-accent">
              {isDeckKind
                ? t_i18n("Presentations wearing this template inherit its palette, fonts, logos and predefined assets live. Give each supported theme its palette in Configuration after creation - or let Generate with AI draft them.")
                : t_i18n("After creation you'll be able to upload assets (fonts, images, logos) and define layouts.")}
            </p>
            <p className="content-compact text-accent/80">
              {t_i18n("Templates are never vectorized - agents find them by kind, the themes they support and aspect ratio, not by semantic search.")}
            </p>
          </div>

          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed template")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed template")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}

          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/templates")}>{t_i18n("Cancel")}</Button>
            <Button onClick={handleCreate} disabled={!name.trim() || create.isPending || aiOff}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create Template")}
            </Button>
          </div>
        </CardContent>
      </Card>

      <TemplateGeneratorDialog
        open={generatorOpen}
        onOpenChange={setGeneratorOpen}
        kind={kind.trim().toLowerCase() || "presentation"}
        name={name}
        current={generated}
        onApply={applyCandidate}
      />
      {generated && (
        <TemplatePreviewDialog
          template={candidateToTemplate(
            { ...generated, name: name || generated.name, extra_metadata: declareSchemes(generated.extra_metadata) },
            [],
            0,
          )}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
        />
      )}
    </div>
  );
}
