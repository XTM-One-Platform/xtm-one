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
 * Drawer extras for the event nodes — the UE Event Dispatcher UX.
 *
 * The three event nodes (`action.emit_event`, `trigger.event`,
 * `util.wait_event`) bind to a DECLARED event type — a named, typed
 * signature — so emitter and listeners can never drift apart on payload
 * shape. This component adds what the generic field renderer cannot:
 *
 * - **Declare** — an inline "new event type" form (name + typed fields),
 *   the same pick-or-create gating the agent node uses.
 * - **Emit** — one variable-aware input per declared field, stored under
 *   `config.mappings.<field>`; the runner renders and type-coerces them
 *   against the declaration at emit time.
 * - **Listen** — the declared fields as `{{node.fields.<name>}}` reference
 *   chips, so wiring downstream nodes needs no guesswork.
 */

import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useCreateFlowEventType, useFlowEventTypes } from "@/hooks/api/useFlowApi";
import { useFormatter } from "@/i18n";
import type { FlowEventField } from "@/types";
import {
  EVENT_TYPE_NAME_MAX_LENGTH,
  normalizeEventTypeName,
} from "./event-type-name";
import type { CanvasNode } from "./graph-adapter";
import type { VariableSuggestion } from "./upstream";
import { VariableInput } from "./VariableInput";

const FIELD_TYPES: FlowEventField["type"][] = [
  "string",
  "number",
  "boolean",
  "json",
];

interface DraftField {
  name: string;
  type: FlowEventField["type"];
  required: boolean;
}

interface DeclaredTypeCreatorProps {
  readOnly: boolean;
  /** The button's label — "Declare a new event type" by default. */
  buttonLabel?: string;
  onCreated: (created: { id: string; name: string }) => void;
}

/**
 * Pick-or-create's CREATE half, shared by every surface that binds to a
 * declared type (event nodes, the Value node's Object mode): name + typed
 * fields + required switches, one POST, and the fresh id handed back.
 */
export function DeclaredTypeCreator({
  readOnly,
  buttonLabel,
  onCreated,
}: DeclaredTypeCreatorProps) {
  const { t_i18n } = useFormatter();
  const createType = useCreateFlowEventType();
  const [creating, setCreating] = useState(false);
  const [draftName, setDraftName] = useState("");
  const [draftFields, setDraftFields] = useState<DraftField[]>([
    { name: "", type: "string", required: true },
  ]);

  const validDraftFields = draftFields.filter(
    (f) => f.name.trim() && /^[A-Za-z_][A-Za-z0-9_]*$/.test(f.name.trim()),
  );
  const eventTypeName = normalizeEventTypeName(draftName);
  const canCreate =
    eventTypeName.length > 0 &&
    validDraftFields.length ===
      draftFields.filter((f) => f.name.trim()).length &&
    !createType.isPending;

  const submitCreate = async () => {
    const created = await createType.mutateAsync({
      name: eventTypeName,
      fields: validDraftFields.map((f) => ({
        name: f.name.trim(),
        type: f.type,
        required: f.required,
      })),
    });
    onCreated({ id: created.id, name: created.name });
    setCreating(false);
    setDraftName("");
    setDraftFields([{ name: "", type: "string", required: true }]);
  };

  if (readOnly) return null;
  if (!creating) {
    return (
      <Button
        variant="outline"
        size="sm"
        className="w-full gap-1.5"
        onClick={() => setCreating(true)}
      >
        <Plus className="h-3.5 w-3.5" />{" "}
        {buttonLabel ?? t_i18n("Declare a new event type")}
      </Button>
    );
  }
  return (
    <Dialog open onOpenChange={(open) => !open && setCreating(false)}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {buttonLabel ?? t_i18n("Declare a new event type")}
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-1.5">
        <Label>{t_i18n("Event type name")}</Label>
        <Input aria-label={t_i18n("Event type name")}
          value={draftName}
          maxLength={EVENT_TYPE_NAME_MAX_LENGTH}
          placeholder={t_i18n("e.g. TicketEscalated")}
          onChange={(e) => setDraftName(e.target.value)}
        />
      </div>
      <div className="space-y-1.5">
        <Label>{t_i18n("Fields")}</Label>
        {draftFields.map((f, i) => (
          <div key={i} className="flex items-center gap-1.5">
            <Input aria-label={t_i18n("Fields")}
              value={f.name}
              placeholder={t_i18n("field_name")}
              className="flex-1 font-mono-plex"
              onChange={(e) =>
                setDraftFields((prev) =>
                  prev.map((row, j) =>
                    j === i ? { ...row, name: e.target.value } : row,
                  ),
                )
              }
            />
            <Select
              value={f.type}
              onValueChange={(v) =>
                setDraftFields((prev) =>
                  prev.map((row, j) =>
                    j === i
                      ? { ...row, type: v as FlowEventField["type"] }
                      : row,
                  ),
                )
              }
            >
              <SelectTrigger className="w-28" aria-label={t_i18n("Type")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FIELD_TYPES.map((ft) => (
                  <SelectItem key={ft} value={ft}>
                    {ft}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Switch
              checked={f.required}
              title={t_i18n("Required")}
              onCheckedChange={(checked) =>
                setDraftFields((prev) =>
                  prev.map((row, j) =>
                    j === i ? { ...row, required: checked } : row,
                  ),
                )
              }
            />
            <Button
              variant="ghost"
              size="icon"
              aria-label={t_i18n("Remove field")}
              className="shrink-0"
              disabled={draftFields.length === 1}
              onClick={() =>
                setDraftFields((prev) => prev.filter((_, j) => j !== i))
              }
            >
              <Trash2 className="h-3.5 w-3.5" />
            </Button>
          </div>
        ))}
        <Button
          variant="ghost"
          className="gap-1"
          onClick={() =>
            setDraftFields((prev) => [
              ...prev,
              { name: "", type: "string", required: false },
            ])
          }
        >
          <Plus className="h-3 w-3" /> {t_i18n("Add field")}
        </Button>
      </div>
      <div className="flex justify-end gap-1.5">
        <Button variant="ghost" onClick={() => setCreating(false)}>
          {t_i18n("Cancel")}
        </Button>
        <Button disabled={!canCreate} onClick={submitCreate}>
          {t_i18n("Create")}
        </Button>
      </div>
      </DialogContent>
    </Dialog>
  );
}

interface EventNodeExtrasProps {
  node: CanvasNode;
  suggestions: VariableSuggestion[];
  readOnly: boolean;
  onChangeConfig: (key: string, value: unknown) => void;
}

export function EventNodeExtras({
  node,
  suggestions,
  readOnly,
  onChangeConfig,
}: EventNodeExtrasProps) {
  const { t_i18n } = useFormatter();
  const { data: eventTypes } = useFlowEventTypes();

  const selectedId =
    typeof node.data.config?.event_type_id === "string"
      ? (node.data.config.event_type_id as string)
      : "";
  const selected = (eventTypes ?? []).find((et) => et.id === selectedId);
  // The On-delegate attachment IS an emitter: same mapping editor, the
  // templates just also see the pseudo-node {{delegation.*}} at runtime.
  const isEmit =
    node.data.kind === "action.emit_event" ||
    node.data.kind === "capability.on_delegate";

  const mappings =
    node.data.config?.mappings &&
    typeof node.data.config.mappings === "object"
      ? (node.data.config.mappings as Record<string, string>)
      : {};


  return (
    <div className="space-y-3">
      {/* Declare — pick-or-create, mirroring the agent node's gating. */}
      <DeclaredTypeCreator
        readOnly={readOnly}
        onCreated={(created) => onChangeConfig("event_type_id", created.id)}
      />

      {/* The bound declaration's signature. */}
      {selected && (
        <div className="space-y-1.5">
          <p className="content-caption font-semibold uppercase tracking-wide text-default-secondary">
            {isEmit ? t_i18n("Event payload") : t_i18n("Event fields")}
          </p>
          {selected.fields.length === 0 && (
            <p className="text-[11px] text-default-secondary">
              {t_i18n("This event type declares no fields.")}
            </p>
          )}
          {isEmit
            ? selected.fields.map((field) => (
                <div key={field.name} className="space-y-1">
                  <Label className="flex items-center gap-1 font-mono-plex">
                    {field.name}
                    <span className="font-sans-plex content-caption text-default-secondary">
                      ({field.type})
                    </span>
                    {field.required && <span className="text-feedback-error-primary">*</span>}
                  </Label>
                  <VariableInput
                    aria-label={field.name}
                    value={mappings[field.name] ?? ""}
                    suggestions={suggestions}
                    disabled={readOnly}
                    placeholder={
                      field.type === "json" ? '{"key": "value"}' : "{{node.output}}"
                    }
                    onChange={(value) =>
                      onChangeConfig("mappings", {
                        ...mappings,
                        [field.name]: value,
                      })
                    }
                  />
                </div>
              ))
            : (
              <div className="flex flex-wrap gap-1">
                {selected.fields.map((field) => (
                  <Badge
                    key={field.name}
                    variant="outline"
                    className="font-mono-plex content-caption"
                    title={`${field.type}${field.required ? " — required" : ""}`}
                  >
                    {`{{${node.id}.fields.${field.name}}}`}
                  </Badge>
                ))}
              </div>
            )}
        </div>
      )}
    </div>
  );
}
