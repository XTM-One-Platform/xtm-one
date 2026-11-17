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

import { useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { Plus, Minus, Building2 } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { BackButton } from "@/components/ui/back-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SettingsField, settingsFieldHintId } from "@/components/ui/settings-form";
import { Switch } from "@/components/ui/switch";
import { TagInput } from "@/components/ui/tag-input";
import { useToast } from "@/components/ui/toast";
import { useAuth } from "@/hooks/useAuth";
import { useCreateCustomTool } from "@/hooks/api/useCustomToolApi";
import type { CustomTool, CustomToolLanguage } from "@/types";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import { FIELD_FOCUS_RING } from "@/lib/field-focus-ring";
import { VariableInsertButton } from "@/components/ui/variable-insert-button";
import { buildSchema, newSchemaRow, PARAM_TYPE_OPTIONS } from "./schema-utils";
import type { SchemaProperty, SchemaPropertyRow } from "./schema-utils";
import { CustomToolCodeGenerator } from "./CustomToolCodeGenerator";
import { useCredentialSharingConfirm } from "@/hooks/useCredentialSharingConfirm";

// ---------------------------------------------------------------------------
// New Tool Form
// ---------------------------------------------------------------------------

export function NewToolForm() {
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  const create = useCreateCustomTool();
  const [consentElement, confirmSharing] = useCredentialSharingConfirm();
  const { toast } = useToast();
  const { t_i18n } = useFormatter();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [iconUrl, setIconUrl] = useState("");
  const [jsFunction, setJsFunction] = useState("");
  const [language, setLanguage] = useState<CustomToolLanguage>("javascript");
  const [timeout, setTimeout] = useState(300);
  const [newTags, setNewTags] = useState<string[]>([]);
  const [isCompanyManaged, setIsCompanyManaged] = useState(false);
  const [properties, setProperties] = useState<SchemaPropertyRow[]>(() => [newSchemaRow()]);

  const jsRef = useRef<HTMLTextAreaElement>(null);
  const addProperty = () => setProperties([...properties, newSchemaRow()]);
  const removeProperty = (idx: number) => setProperties(properties.filter((_, i) => i !== idx));
  const updateProperty = (idx: number, field: keyof SchemaProperty, value: string | boolean) => {
    const next = [...properties];
    next[idx] = { ...next[idx], [field]: value };
    setProperties(next);
  };

  const handleCreate = async () => {
    if (!name.trim()) return;
    const validProps = properties.filter((p) => p.name.trim());
    // Creating it company-managed runs its code for everyone.
    if (isCompanyManaged && !(await confirmSharing({
      entity_type: "custom_tool",
      change: { name, is_company_managed: true, js_function: jsFunction, description },
    }))) return;
    create.mutate(
      {
        name,
        description,
        icon_url: iconUrl || undefined,
        tags: newTags.length > 0 ? newTags : undefined,
        input_schema: validProps.length > 0 ? buildSchema(validProps) : undefined,
        js_function: jsFunction || undefined,
        language,
        timeout,
        is_company_managed: isCompanyManaged || undefined,
      },
      {
        onSuccess: (data) => {
          const tool = data as CustomTool;
          toast({ title: t_i18n("Tool created"), description: t_i18n("{name} has been created.", { name: tool.name }), variant: "success" });
          navigate(`/tools/${tool.id}`);
        },
      },
    );
  };

  return (
    <>
      {consentElement}
    <div className="space-y-6">
      <BackButton to="/tools/custom" label="Back to Tools" />

      <div>
        <h1 className="text-2xl font-bold">{t_i18n("Create Custom Tool")}</h1>
        <p className="mt-1 content-base text-default-secondary">
          {t_i18n("Define a tool with input parameters and a JavaScript or Python function. Agents will be able to call this tool during assignment execution.")}
        </p>
      </div>

      <Card>
        <CardContent className="pt-6 space-y-5">
          {/* Name / Icon URL / Tags */}
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <SettingsField id="new-tool-name" label={t_i18n("Tool Name")} required helper={t_i18n("The name the AI model calls the tool by.")}>
              <Input id="new-tool-name" aria-label={t_i18n("Tool Name")} aria-describedby={settingsFieldHintId("new-tool-name")} placeholder={t_i18n("e.g. notion_search")} value={name} onChange={(e) => setName(e.target.value)} className="font-mono-plex" />
            </SettingsField>
            <SettingsField id="new-tool-icon-url" label={t_i18n("Icon URL (optional)")} helper={t_i18n("An image shown beside the tool in lists. Empty: the default icon.")}>
              <Input id="new-tool-icon-url" aria-label={t_i18n("Icon URL (optional)")} aria-describedby={settingsFieldHintId("new-tool-icon-url")} placeholder="https://example.com/icon.svg" value={iconUrl} onChange={(e) => setIconUrl(e.target.value)} />
            </SettingsField>
            <SettingsField id="new-tool-tags" className="sm:col-span-2 xl:col-span-1" label={t_i18n("Tags")} helper={t_i18n("Words to find and group it in lists. Press Enter after each tag.")}>
              <TagInput id="new-tool-tags" aria-label={t_i18n("Tags")} aria-describedby={settingsFieldHintId("new-tool-tags")} value={newTags} onChange={setNewTags} placeholder={t_i18n("Add tags…")} />
            </SettingsField>
          </div>

          <SettingsField id="new-tool-description" label={t_i18n("Description")} helper={t_i18n("Sent to the AI model to decide when to call the tool: say what it does and returns.")}>
            <textarea
              id="new-tool-description"
              aria-label={t_i18n("Description")}
              aria-describedby={settingsFieldHintId("new-tool-description")}
              className={`flex min-h-[80px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
              placeholder={t_i18n("Describe what this tool does: this text is shown to the AI model.")}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </SettingsField>

          {/* Input Schema */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label className="block text-default-primary">{t_i18n("Input Parameters")}</Label>
              <Button variant="ghost" size="sm" className="h-6 gap-1 content-compact text-default-secondary" onClick={addProperty}>
                <Plus className="h-3 w-3" /> {t_i18n("Add Parameter")}
              </Button>
            </div>

            <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
              {/* Header */}
              <div className="grid grid-cols-[1fr_120px_1fr_80px_40px] gap-2 px-3 py-2 bg-elevation-hover content-compact font-medium text-default-secondary">
                <span>{t_i18n("Name")}</span>
                <span>{t_i18n("Type")}</span>
                <span>{t_i18n("Description")}</span>
                <span>{t_i18n("Required")}</span>
                <span></span>
              </div>

              {properties.map((prop, idx) => (
                <div key={prop.rowId} className="grid grid-cols-[1fr_120px_1fr_80px_40px] gap-2 px-3 py-2 border-t border-elevation-subtle-soft items-center">
                  <Input aria-label={t_i18n("Name")}
                    value={prop.name}
                    onChange={(e) => updateProperty(idx, "name", e.target.value)}
                    placeholder="param_name"
                    className="font-mono-plex"
                  />
                  <Select value={prop.type} onValueChange={(v) => updateProperty(idx, "type", v)}>
                    <SelectTrigger aria-label={t_i18n("Type")} className="w-full min-w-0">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PARAM_TYPE_OPTIONS.map((t) => (
                        <SelectItem key={t} value={t}>{t}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Input aria-label={t_i18n("Description")}
                    value={prop.description}
                    onChange={(e) => updateProperty(idx, "description", e.target.value)}
                    placeholder={t_i18n("Description")}
                  />
                  <div className="flex justify-center">
                    <Switch aria-label={t_i18n("Required")}
                      checked={prop.required}
                      onCheckedChange={(v) => updateProperty(idx, "required", v)}
                    />
                  </div>
                  <Button aria-label={t_i18n("Remove parameter")} variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={() => removeProperty(idx)}>
                    <Minus className="h-3 w-3 text-default-secondary" />
                  </Button>
                </div>
              ))}
            </div>
          </div>

          {/* Language + Function */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <Label className="block text-default-primary">{t_i18n("Language")}</Label>
              <div className="flex rounded-sm border border-elevation-subtle-soft overflow-hidden">
                {(["javascript", "python"] as const).map((lang) => (
                  <button
                    key={lang}
                    type="button"
                    className={cn(
                      "px-3 py-1.5 content-compact font-medium transition-colors",
                      language === lang ? "bg-accent/10 text-accent" : "text-default-secondary hover:text-default-primary hover:bg-elevation-hover",
                    )}
                    onClick={() => setLanguage(lang)}
                  >
                    {lang === "javascript" ? "JavaScript" : "Python"}
                  </button>
                ))}
              </div>
            </div>

            <div className="space-y-1.5">
              <div className="flex items-center justify-between">
                <Label id="new-tool-function-label" htmlFor="new-tool-function" className="block text-default-primary">
                  {language === "javascript" ? t_i18n("JavaScript Function") : t_i18n("Python Function")}
                </Label>
                <div className="flex items-center gap-2">
                  <VariableInsertButton textareaRef={jsRef} value={jsFunction} onChange={setJsFunction} />
                  <CustomToolCodeGenerator
                    language={language}
                    code={jsFunction}
                    onGenerated={(result) => setJsFunction(result.code)}
                    toolName={name}
                    toolDescription={description}
                    parameters={properties}
                  />
                </div>
              </div>
              <p id="new-tool-function-hint" className="content-compact text-default-secondary">
                {language === "javascript" ? (
                  <>{t_i18n("Reference input parameters as")} <code className="text-accent">$param_name</code>. {t_i18n("Use")} <code className="text-accent">fetch</code> {t_i18n("for HTTP calls. Output goes to stdout.")}</>
                ) : (
                  <>{t_i18n("Reference input parameters as")} <code className="text-accent">param_name</code> {t_i18n("(plain variables).")} {t_i18n("Use")} <code className="text-accent">print()</code> {t_i18n("for output. Standard library + requests-free HTTP via")} <code className="text-accent">urllib</code> {t_i18n("are not available: use the tool for data processing and formatting.")}</>
                )}
              </p>
              <textarea
                ref={jsRef}
                id="new-tool-function"
                aria-describedby="new-tool-function-hint"
                className={`flex min-h-[200px] w-full rounded-sm border border-elevation-subtle bg-elevation-default px-3 py-2 content-base font-mono-plex placeholder:text-default-secondary ${FIELD_FOCUS_RING}`}
                placeholder={language === "javascript"
                  ? `const response = await fetch('https://api.example.com/search', {\n  method: 'POST',\n  headers: { 'Content-Type': 'application/json' },\n  body: JSON.stringify({ query: $keyword })\n});\nconst data = await response.json();\nconsole.log(JSON.stringify(data, null, 2));`
                  : `import json\n\nresult = {"query": keyword, "status": "processed"}\nprint(json.dumps(result, indent=2))`}
                value={jsFunction}
                onChange={(e) => setJsFunction(e.target.value)}
              />
            </div>
          </div>

          {/* Execution timeout */}
          <SettingsField
            id="new-tool-timeout"
            label={t_i18n("Execution Timeout (seconds)")}
            helper={t_i18n("Maximum time the tool is allowed to run before being killed. Default: 300s (5 min).")}
            info={t_i18n("In seconds, from 1 to 3600. A run still going at that point is stopped.")}
          >
            <Input id="new-tool-timeout" aria-label={t_i18n("Execution Timeout (seconds)")} aria-describedby={settingsFieldHintId("new-tool-timeout")}
              type="number"
              min={1}
              max={3600}
              value={timeout}
              onChange={(e) => setTimeout(Math.max(1, Math.min(3600, Number(e.target.value) || 300)))}
              className="content-base font-mono-plex w-40"
            />
          </SettingsField>

          {/* Company-managed toggle (admin only) */}
          {isAdmin && (
            <div className="flex items-center justify-between rounded-sm bg-elevation-hover p-3">
              <div className="flex items-center gap-2">
                <Building2 className="h-4 w-4 text-accent" />
                <div>
                  <p className="content-base font-medium">{t_i18n("Company-managed tool")}</p>
                  <p className="content-compact text-default-secondary">{t_i18n("Shared with all users. Only admins can edit.")}</p>
                </div>
              </div>
              <Switch aria-label={t_i18n("Company-managed tool")} checked={isCompanyManaged} onCheckedChange={setIsCompanyManaged} />
            </div>
          )}

          {/* Actions */}
          <div className="flex justify-end gap-3">
            <Button variant="ghost" onClick={() => navigate("/tools/custom")}>{t_i18n("Cancel")}</Button>
            {/* No AI gate: a Custom tool is a sandboxed script, and
                ``action.script`` runs one from a deterministic flow, so
                authoring must survive AI being off (FR-014). */}
            <Button onClick={handleCreate} disabled={!name.trim() || create.isPending}>
              {create.isPending ? t_i18n("Creating...") : t_i18n("Create Tool")}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
    </>
  );
}
