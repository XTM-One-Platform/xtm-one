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

import { useId, useState } from "react";
import { Eye, Moon, PanelBottom, Sparkles, Sun, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { TagInput } from "@/components/ui/tag-input";
import { Badge } from "@/components/ui/badge";
import { DisclosureSummary } from "@/components/ui/disclosure";
import { useToast } from "@/components/ui/toast";
import { EntityConfigCard } from "@/components/ui/entity-config-card";
import { TemplateGeneratorDialog, PaletteStrip } from "@/components/presentation/TemplateGeneratorDialog";
import { chromeExtraSummary, templateChromeOf } from "@/components/presentation/chrome-pattern";
import { TemplateDesignEditor } from "@/components/presentation/TemplateDesignEditor";
import { TemplateKindSelect } from "@/components/presentation/TemplateKindSelect";
import { TemplatePreviewDialog } from "@/components/presentation/TemplatePreviewDialog";
import { draftPreviewTemplate } from "@/components/presentation/draft-template";
import { previewThemeConfigs, templateSupportedSchemes, THEME_SCHEMES } from "@/components/presentation/previewSpec";
import { useSchemeLabel } from "@/components/presentation/scheme-label";
import { useUserShareGrants } from "@/hooks/useUserShareGrants";
import { useUpdateTemplateContainer } from "@/hooks/api/useTemplateContainerApi";
import { useAiActionDisabled } from "@/hooks/useAiDisabled";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { templateKindLabel } from "@/lib/constants";
import type { TemplateCandidate, TemplateContainerDetail } from "@/types";

/** Pretty-print a JSON value for the editor, or return "" for an empty one so
 *  the textarea starts blank rather than showing `{}` / `[]` noise. */
function toEditorText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value) && value.length === 0) return "";
  if (typeof value === "object" && Object.keys(value as object).length === 0) return "";
  return JSON.stringify(value, null, 2);
}

type JsonFieldState = { text: string; error: string | null };

/** Parse an editor field, returning the value to send or an error to display.
 *  An empty field means "clear it" — `{}` for objects, `[]` for arrays — which
 *  is why the expected container kind has to be passed in explicitly. */
function parseJsonField(text: string, expect: "object" | "array"): { value: unknown; error: string | null } {
  const trimmed = text.trim();
  if (!trimmed) return { value: expect === "array" ? [] : {}, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (err) {
    return { value: null, error: err instanceof Error ? err.message : "Invalid JSON" };
  }
  if (expect === "array" && !Array.isArray(parsed)) {
    return { value: null, error: "Expected a JSON array" };
  }
  if (expect === "object" && (typeof parsed !== "object" || parsed === null || Array.isArray(parsed))) {
    return { value: null, error: "Expected a JSON object" };
  }
  return { value: parsed, error: null };
}

/** Raw-JSON editor for the consumer-owned structured fields.
 *  A structured form is not designable until a consumer feature pins down the
 *  layout/theme schema — and inventing one here would silently constrain what
 *  the container is allowed to hold. Validation on blur keeps it safe. */
function JsonField({
  label, hint, state, expect, onChange, rows = 8,
}: {
  label: string;
  hint: string;
  state: JsonFieldState;
  expect: "object" | "array";
  onChange: (next: JsonFieldState) => void;
  rows?: number;
}) {
  const { t_i18n } = useFormatter();
  const fieldId = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={fieldId} className="content-compact text-default-secondary">{t_i18n(label)}</label>
      <textarea
        id={fieldId}
        className={`flex w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 font-mono-plex content-compact placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
        rows={rows}
        value={state.text}
        spellCheck={false}
        onChange={(e) => onChange({ text: e.target.value, error: null })}
        onBlur={(e) => onChange({ text: e.target.value, error: parseJsonField(e.target.value, expect).error })}
      />
      {state.error ? (
        <p className="text-[11px] text-feedback-error-primary">{state.error}</p>
      ) : (
        <p className="content-caption text-default-secondary">{t_i18n(hint)}</p>
      )}
    </div>
  );
}

function ReadOnlyJson({ label, value }: { label: string; value: unknown }) {
  const { t_i18n } = useFormatter();
  const text = toEditorText(value);
  if (!text) return null;
  return (
    <div>
      <div className="content-compact text-default-secondary mb-1">{t_i18n(label)}</div>
      <pre className="rounded-sm bg-elevation-hover px-3 py-2 content-compact text-accent overflow-x-auto max-h-[300px] overflow-y-auto whitespace-pre-wrap">
        {text}
      </pre>
    </div>
  );
}

function ConfigurationCard({ template, canManage }: { template: TemplateContainerDetail; canManage: boolean }) {
  const { t_i18n } = useFormatter();
  const schemeLabel = useSchemeLabel();
  const update = useUpdateTemplateContainer();
  const { toast } = useToast();
  const [editing, setEditing] = useState(false);

  const [editName, setEditName] = useState(template.name);
  const [editDescription, setEditDescription] = useState(template.description ?? "");
  const [editKind, setEditKind] = useState(template.kind);
  // The free-text `theme` LABEL is legacy: the themes a template
  // supports are read from its palettes. The field is only shown, as "Search
  // label (legacy)", on a template that still carries one — so it can be
  // cleared — and never offered on one that does not.
  const [editLegacyLabel, setEditLegacyLabel] = useState(template.theme ?? "");
  const [editAspectRatio, setEditAspectRatio] = useState(template.aspect_ratio ?? "");
  const [editTags, setEditTags] = useState<string[]>(template.tags || []);
  const [editThemeConfig, setEditThemeConfig] = useState<JsonFieldState>({ text: toEditorText(template.theme_config), error: null });
  const [editLayouts, setEditLayouts] = useState<JsonFieldState>({ text: toEditorText(template.layouts), error: null });
  const [editExtraMetadata, setEditExtraMetadata] = useState<JsonFieldState>({ text: toEditorText(template.extra_metadata), error: null });
  const [editCompanyManaged, setEditCompanyManaged] = useState(template.is_company_managed);
  const [editSharedGroupIds, setEditSharedGroupIds] = useState<string[] | null>(template.shared_group_ids ?? null);
  const [editSharedGroupPermissions, setEditSharedGroupPermissions] = useState<Record<string, string> | null>(template.shared_group_permissions ?? null);
  // Per-user grants + access restriction.
  const shareGrants = useUserShareGrants(template);
  // "Generate with AI" fills the edit fields from a brief; the user
  // still reviews and saves, so nothing is written by the generator itself.
  const aiOff = useAiActionDisabled();
  const [generatorOpen, setGeneratorOpen] = useState(false);
  // The header's Preview shows the SAVED template; this one shows the design
  // being edited, so a palette change is judged before it is saved.
  const [draftPreviewOpen, setDraftPreviewOpen] = useState(false);
  const nameFieldId = useId();
  const aspectRatioFieldId = useId();

  const applyCandidate = (candidate: TemplateCandidate) => {
    setEditName(candidate.name);
    setEditDescription(candidate.description ?? "");
    setEditKind(candidate.kind);
    setEditAspectRatio(candidate.aspect_ratio ?? "");
    setEditTags(candidate.tags);
    setEditThemeConfig({ text: toEditorText(candidate.theme_config), error: null });
    setEditLayouts({ text: toEditorText(candidate.layouts), error: null });
    setEditExtraMetadata({ text: toEditorText(candidate.extra_metadata), error: null });
  };

  /** The design currently in the editor, as the generator's refinement base. */
  const currentDesign = (): Partial<TemplateCandidate> => ({
    name: editName,
    description: editDescription || null,
    kind: editKind,
    theme: null,
    aspect_ratio: editAspectRatio || null,
    tags: editTags,
    theme_config: (parseJsonField(editThemeConfig.text, "object").value as Record<string, string> | null) ?? {},
    layouts: (parseJsonField(editLayouts.text, "array").value as Record<string, unknown>[] | null) ?? [],
    extra_metadata: (parseJsonField(editExtraMetadata.text, "object").value as Record<string, unknown> | null) ?? {},
  });

  const startEditing = () => {
    setEditing(true);
    setEditName(template.name);
    setEditDescription(template.description ?? "");
    setEditKind(template.kind);
    setEditLegacyLabel(template.theme ?? "");
    setEditAspectRatio(template.aspect_ratio ?? "");
    setEditTags(template.tags || []);
    setEditThemeConfig({ text: toEditorText(template.theme_config), error: null });
    setEditLayouts({ text: toEditorText(template.layouts), error: null });
    setEditExtraMetadata({ text: toEditorText(template.extra_metadata), error: null });
    setEditCompanyManaged(template.is_company_managed);
    setEditSharedGroupIds(template.shared_group_ids ?? null);
    setEditSharedGroupPermissions(template.shared_group_permissions ?? null);
    shareGrants.reset();
  };

  const themeConfigParsed = parseJsonField(editThemeConfig.text, "object");
  const layoutsParsed = parseJsonField(editLayouts.text, "array");
  const extraMetadataParsed = parseJsonField(editExtraMetadata.text, "object");
  const jsonValid = !themeConfigParsed.error && !layoutsParsed.error && !extraMetadataParsed.error;

  const isDeckKind = editKind.trim().toLowerCase() === "presentation";
  // The structured design editor works on the parsed documents and writes the
  // serialised text back, so the JSON fields under "Advanced" stay the single
  // source of truth and never disagree with what the palette editor shows.
  const designEditable = !themeConfigParsed.error && !extraMetadataParsed.error;
  const designThemeConfig = (themeConfigParsed.value as Record<string, unknown> | null) ?? {};
  const designExtraMetadata = (extraMetadataParsed.value as Record<string, unknown> | null) ?? {};
  // What "Preview the unsaved design" shows: the template as saving would
  // write it - name, aspect ratio, layouts, palettes and deck defaults.
  const draftForPreview =
    draftPreviewOpen && designEditable
      ? draftPreviewTemplate(template, {
          name: editName,
          aspectRatio: editAspectRatio,
          layouts: layoutsParsed.error ? null : layoutsParsed.value,
          themeConfig: designThemeConfig,
          extraMetadata: designExtraMetadata,
        })
      : null;
  const readPalettes = template.kind === "presentation" ? previewThemeConfigs(template) : null;
  // The footer / header the template declares, shown next to the
  // palettes it is as much a part of the design as they are.
  const readChrome = template.kind === "presentation" ? templateChromeOf(template) : null;

  const handleSave = () => {
    if (!editName.trim() || !jsonValid) {
      // Surface the errors the user has not seen yet (a field edited but never
      // blurred still shows error: null), instead of failing silently.
      setEditThemeConfig((s) => ({ ...s, error: themeConfigParsed.error }));
      setEditLayouts((s) => ({ ...s, error: layoutsParsed.error }));
      setEditExtraMetadata((s) => ({ ...s, error: extraMetadataParsed.error }));
      return;
    }
    const resolvedGroupIds = editCompanyManaged
      ? null
      : (editSharedGroupIds && editSharedGroupIds.length > 0 ? editSharedGroupIds : null);
    const resolvedGroupPermissions = editCompanyManaged
      ? null
      : (editSharedGroupPermissions && Object.keys(editSharedGroupPermissions).length > 0 ? editSharedGroupPermissions : null);

    update.mutate(
      {
        id: template.id,
        name: editName,
        description: editDescription,
        // Sent only when it changed: a legacy kind (`asset-library`) is listed
        // in place so the row keeps working, and the backend treats an echo as
        // a no-op anyway - but there is no reason to send what did not move.
        ...(editKind.trim().toLowerCase() !== template.kind.trim().toLowerCase()
          ? { kind: editKind.trim().toLowerCase() || undefined }
          : {}),
        // Only a template that still carries the legacy label sends it back
        // (possibly cleared to ""); the field no longer exists otherwise.
        ...(template.theme ? { theme: editLegacyLabel.trim() } : {}),
        aspect_ratio: editAspectRatio.trim(),
        tags: editTags,
        theme_config: themeConfigParsed.value as Record<string, unknown>,
        layouts: layoutsParsed.value as Record<string, unknown>[],
        extra_metadata: extraMetadataParsed.value as Record<string, unknown>,
        is_company_managed: editCompanyManaged,
        // Sent as explicit ``null`` (never omitted) so the backend's
        // ``"shared_group_ids" in model_fields_set`` check fires and actually
        // clears the sharing — omitting the key would make "remove all group
        // shares" a silent no-op.
        shared_group_ids: resolvedGroupIds,
        shared_group_permissions: resolvedGroupPermissions,
        ...shareGrants.toPayload(editCompanyManaged),
      },
      {
        onSuccess: () => {
          toast({ title: t_i18n("Template updated"), description: t_i18n("Configuration saved successfully."), variant: "success" });
          setEditing(false);
        },
      },
    );
  };

  return (
    <EntityConfigCard
      entityLabel="template"
      isCompanyManaged={template.is_company_managed}
      isBuiltin={template.is_builtin}
      userId={template.user_id}
      sharedGroupIds={template.shared_group_ids}
      ownerName={template.owner_name}
      canManage={canManage}
      editing={editing}
      onStartEditing={startEditing}
      onCancel={() => setEditing(false)}
      onSave={handleSave}
      isSaving={update.isPending}
      canSave={!!editName.trim() && jsonValid}
      editCompanyManaged={editCompanyManaged}
      onCompanyManagedChange={setEditCompanyManaged}
      editSharedGroupIds={editSharedGroupIds}
      onSharedGroupIdsChange={setEditSharedGroupIds}
      editSharedGroupPermissions={editSharedGroupPermissions}
      onSharedGroupPermissionsChange={setEditSharedGroupPermissions}
      userSharing={shareGrants.props}
      onTransferOwner={(userId) =>
        update.mutate(
          { id: template.id, user_id: userId },
          { onSuccess: () => toast({ title: t_i18n("Owner updated"), variant: "success" }) },
        )
      }
      readView={
        <>
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Name")}</div>
              <p className="content-base font-medium">{template.name}</p>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Description")}</div>
              <p className="content-base">{template.description || "—"}</p>
            </div>
          </div>
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Kind")}</div>
              <Badge variant="cyan" className="content-caption">{t_i18n(templateKindLabel(template.kind))}</Badge>
            </div>
            <div>
              <div className="content-compact text-default-secondary mb-1">{t_i18n("Aspect ratio")}</div>
              <p className="content-base">{template.aspect_ratio || "—"}</p>
            </div>
            {template.theme && (
              <div>
                <div className="content-compact text-default-secondary mb-1">{t_i18n("Search label (legacy)")}</div>
                <p className="content-base">{template.theme}</p>
              </div>
            )}
          </div>
          {readPalettes && (
            <div className="space-y-2">
              <div className="content-compact text-default-secondary">{t_i18n("Design")}</div>
              <div className="grid gap-2 sm:grid-cols-2">
                {THEME_SCHEMES.map((scheme) => {
                  const supported = template.supported_schemes.includes(scheme);
                  const Icon = scheme === "dark" ? Moon : Sun;
                  return (
                    <div key={scheme} className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <span className="flex items-center gap-1.5 content-compact font-medium">
                          <Icon className="h-3.5 w-3.5 text-default-secondary" />
                          {t_i18n("{scheme} theme", { scheme: schemeLabel(scheme) })}
                        </span>
                        <Badge variant={supported ? "cyan" : "outline"} className="text-[9px] px-1.5 py-0">
                          {supported ? t_i18n("Supported") : t_i18n("Not supported")}
                        </Badge>
                      </div>
                      {supported ? (
                        <PaletteStrip themeConfig={readPalettes[scheme]} size="sm" />
                      ) : (
                        <p className="text-[11px] text-default-secondary">
                          {t_i18n("Decks wearing this template cannot use this theme.")}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="space-y-1 rounded-sm border border-elevation-subtle-soft p-2.5">
                <span className="flex items-center gap-1.5 content-compact font-medium">
                  <PanelBottom className="h-3.5 w-3.5 text-default-secondary" />
                  {t_i18n("Header & footer")}
                </span>
                {readChrome ? (
                  <>
                    {readChrome.footer && (
                      <p className="content-compact">
                        <span className="text-default-secondary">{t_i18n("Footer")}: </span>
                        <span className="font-mono-plex">{readChrome.footer}</span>
                      </p>
                    )}
                    {readChrome.header && (
                      <p className="content-compact">
                        <span className="text-default-secondary">{t_i18n("Header")}: </span>
                        <span className="font-mono-plex">{readChrome.header}</span>
                      </p>
                    )}
                    {/* The right footer and the emblem keys. */}
                    {chromeExtraSummary(readChrome).map((line) => (
                      <p key={line.label} className="content-compact" data-testid="template-chrome-summary-line">
                        <span className="text-default-secondary">{t_i18n(line.label)}: </span>
                        {line.literal ? (
                          <span className="font-mono-plex">{line.value}</span>
                        ) : (
                          <span>{t_i18n(line.value, line.values)}</span>
                        )}
                      </p>
                    ))}
                    {readChrome.variables.length > 0 && (
                      <div className="flex flex-wrap gap-1.5 pt-0.5">
                        {readChrome.variables.map((variable) => (
                          <Badge key={variable.key} variant="outline" className="content-caption gap-1">
                            <span className="font-mono-plex">{`{{${variable.key}}}`}</span>
                            <span className="text-default-secondary">{variable.label}</span>
                            {variable.default && <span className="text-default-secondary">= {variable.default}</span>}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </>
                ) : (
                  <p className="text-[11px] text-default-secondary">
                    {t_i18n("No pattern: a deck wearing this template writes its own footer (event and footer text) in its Header & footer settings.")}
                  </p>
                )}
              </div>
            </div>
          )}
          <details className="group rounded-sm border border-elevation-subtle-soft">
            <DisclosureSummary size="block" className="select-none px-3 py-2 content-compact text-default-secondary">
              {t_i18n("Advanced (JSON)")}
            </DisclosureSummary>
            <div className="space-y-3 border-t border-elevation-subtle-soft p-3">
              <ReadOnlyJson label="Theme configuration" value={template.theme_config} />
              <ReadOnlyJson label="Layouts" value={template.layouts} />
              <ReadOnlyJson label="Extra metadata" value={template.extra_metadata} />
            </div>
          </details>
        </>
      }
      editView={
        <>
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-sm bg-accent/5 p-3">
            <p className="text-[11px] text-accent">
              {t_i18n("Describe a style and let the assistant fill the palette, layout catalog and guidance below - or refine what is already here.")}
            </p>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="gap-1.5 text-accent hover:text-accent/80"
              onClick={() => setGeneratorOpen(true)}
              disabled={aiOff}
            >
              <Sparkles className="h-3.5 w-3.5" /> {t_i18n("Generate with AI")}
            </Button>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label htmlFor={nameFieldId} className="content-compact text-default-secondary">{t_i18n("Template Name")}</label>
              <Input aria-label={t_i18n("Template Name")} id={nameFieldId} value={editName} onChange={(e) => setEditName(e.target.value)} className="h-8 content-base" />
            </div>
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Tags")}</label>
              <TagInput value={editTags} onChange={setEditTags} placeholder={t_i18n("Add tags…")} />
            </div>
          </div>

          <div className="space-y-1">
            <label className="content-compact text-default-secondary">{t_i18n("Description")}</label>
            <textarea
              className={`flex min-h-[60px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              value={editDescription}
              onChange={(e) => setEditDescription(e.target.value)}
            />
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Kind")}</label>
              {/* A closed choice: the backend refuses any other kind
                  with a 422, and a legacy value is offered only so this row
                  can be moved off it. */}
              <TemplateKindSelect value={editKind} onChange={setEditKind} className="h-8 content-base" />
            </div>
            <div className="space-y-1">
              <label htmlFor={aspectRatioFieldId} className="content-compact text-default-secondary">{t_i18n("Aspect ratio")}</label>
              <Input aria-label={t_i18n("Aspect ratio")} id={aspectRatioFieldId} value={editAspectRatio} onChange={(e) => setEditAspectRatio(e.target.value)} className="h-8 content-base" placeholder={t_i18n("e.g. 16:9")} />
            </div>
          </div>

          {template.theme && (
            <div className="space-y-1">
              <label className="content-compact text-default-secondary">{t_i18n("Search label (legacy)")}</label>
              <div className="flex items-center gap-2">
                <Input aria-label={t_i18n("Search label (legacy)")} value={editLegacyLabel} onChange={(e) => setEditLegacyLabel(e.target.value)} className="h-8 content-base" />
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1 text-default-secondary"
                  onClick={() => setEditLegacyLabel("")}
                  disabled={!editLegacyLabel}
                >
                  <X className="h-3.5 w-3.5" /> {t_i18n("Clear")}
                </Button>
              </div>
              <p className="text-[11px] text-default-secondary">
                {t_i18n("Older agent instructions may still look templates up by this word. It no longer changes the design; clear it once nothing depends on it.")}
              </p>
            </div>
          )}

          <div className="rounded-sm bg-elevation-hover p-3">
            <p className="text-[11px] text-default-secondary">
              {isDeckKind
                ? t_i18n("Agents find this template by kind, aspect ratio and the themes it supports (below). Changing the kind or the aspect ratio changes which templates a saved agent instruction finds.")
                : t_i18n("Agents find this template by kind and aspect ratio. Changing either changes which templates a saved agent instruction finds.")}
            </p>
          </div>

          {isDeckKind && (
            <div className="space-y-2">
              <div className="flex items-center justify-between gap-2">
                <label className="content-compact text-default-secondary">{t_i18n("Design")}</label>
                {designEditable && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-7 gap-1.5 content-compact"
                    onClick={() => setDraftPreviewOpen(true)}
                  >
                    <Eye className="h-3.5 w-3.5" /> {t_i18n("Preview the unsaved design")}
                  </Button>
                )}
              </div>
              {designEditable ? (
                <TemplateDesignEditor
                  themeConfig={designThemeConfig}
                  extraMetadata={designExtraMetadata}
                  onThemeConfigChange={(next) => setEditThemeConfig({ text: toEditorText(next), error: null })}
                  onExtraMetadataChange={(next) => setEditExtraMetadata({ text: toEditorText(next), error: null })}
                />
              ) : (
                <p className="rounded-sm bg-feedback-error-primary/10 px-3 py-2 text-[11px] text-feedback-error-primary">
                  {t_i18n("Fix the JSON under Advanced to edit the design here.")}
                </p>
              )}
            </div>
          )}

          <details className="group rounded-sm border border-elevation-subtle-soft" open={!isDeckKind}>
            <DisclosureSummary size="block" className="select-none px-3 py-2 content-compact text-default-secondary">
              {t_i18n("Advanced (JSON)")}
            </DisclosureSummary>
            <div className="space-y-3 border-t border-elevation-subtle-soft p-3">
              <JsonField
                label="Theme configuration"
                hint={isDeckKind
                  ? "JSON object - the base palette, font families and logo file ids the Design editor above writes. Leave empty to clear."
                  : "JSON object - palette, font families, spacing. Consumed by the rendering feature; leave empty to clear."}
                state={editThemeConfig}
                expect="object"
                onChange={setEditThemeConfig}
              />
              <JsonField
                label="Layouts"
                hint="JSON array of layout definitions, each ideally with an id. The structure is owned by the consumer feature."
                state={editLayouts}
                expect="array"
                onChange={setEditLayouts}
                rows={12}
              />
              <JsonField
                label="Extra metadata"
                hint={isDeckKind
                  ? "JSON object - supported_schemes (the themes declared above), max_slides (how many slides a deck of this template may hold; 100 when unset, up to 300), heroes (the template's own tiled covers - mosaic, cascade, horizon, pillar - for a brand whose design kit draws them), theme_variants (the other theme's palette), deck_defaults, guidance and chrome (the header & footer pattern), plus any consumer-specific attribute."
                  : editKind === "video"
                    ? "JSON object - supported_schemes, theme_variants (the other theme's palette), guidance, and video: aspect_ratios, defaults (theme mode / background / watermark, default_transition, captions, voiceover), scene_defaults and overlay_defaults per type, intro and outro ({required, scene}), brand_name (the name the logo spells, when it is not the template's own: a card drawing the logo never writes it again) and locked (the paths a video always takes from the template, theme.palette for the colours and logo)."
                    : "JSON object for consumer-specific attributes that do not deserve their own field."}
                state={editExtraMetadata}
                expect="object"
                onChange={setEditExtraMetadata}
                rows={6}
              />
            </div>
          </details>

          {generatorOpen && (
            <TemplateGeneratorDialog
              open={generatorOpen}
              onOpenChange={setGeneratorOpen}
              kind={editKind.trim().toLowerCase() || "presentation"}
              name={editName}
              current={currentDesign()}
              assets={template.assets}
              onApply={applyCandidate}
            />
          )}
          {draftForPreview && (
            <TemplatePreviewDialog
              template={{ ...draftForPreview, supported_schemes: templateSupportedSchemes(draftForPreview) }}
              open={draftPreviewOpen}
              onOpenChange={setDraftPreviewOpen}
            />
          )}
        </>
      }
    />
  );
}

export function ConfigTab({ template, canManage }: { template: TemplateContainerDetail; canManage: boolean }) {
  return (
    <div className="space-y-6">
      <ConfigurationCard template={template} canManage={canManage} />
    </div>
  );
}
