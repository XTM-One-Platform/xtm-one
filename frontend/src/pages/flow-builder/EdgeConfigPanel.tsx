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
 * The edge configuration drawer.
 *
 * Clicking an edge opens this panel: a label, the drawn shape (bezier /
 * straight / step / smooth step), and — for handover edges — the handover
 * type, whose value IS the edge label (the walker routes on it).
 */

import { Trash2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { HANDOVER_TYPES, HANDOVER_TYPE_CONFIG } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import type { CanvasEdge } from "./graph-adapter";
import { EDGE_STROKE } from "./node-visuals";

const PATH_STYLES = [
  { value: "bezier", label: "Default (bezier)" },
  { value: "straight", label: "Straight" },
  { value: "step", label: "Step" },
  { value: "smoothstep", label: "Smooth step" },
] as const;

interface EdgeConfigPanelProps {
  edge: CanvasEdge;
  /** Labels of the source/target nodes, for the header. */
  sourceLabel: string;
  targetLabel: string;
  readOnly?: boolean;
  onClose: () => void;
  onChange: (patch: Partial<CanvasEdge> & { data?: CanvasEdge["data"] }) => void;
  onDelete: () => void;
}

export function EdgeConfigPanel({
  edge,
  sourceLabel,
  targetLabel,
  readOnly,
  onClose,
  onChange,
  onDelete,
}: EdgeConfigPanelProps) {
  const { t_i18n } = useFormatter();
  const kind = edge.data?.kind ?? "data";
  const isHandover = kind === "handover";
  const stroke = EDGE_STROKE[kind] ?? EDGE_STROKE.data;

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-elevation-subtle layer-1 bg-elevation-default">
      <div className="flex items-start gap-2.5 border-b border-elevation-subtle p-3">
        <span
          className="mt-2 h-1 w-8 shrink-0 rounded-full"
          style={{ background: stroke }}
        />
        <div className="min-w-0 flex-1">
          <p className="truncate content-base font-medium">
            {sourceLabel} → {targetLabel}
          </p>
          <p className="truncate font-mono-plex content-caption text-default-secondary">
            {edge.id} · {kind}
          </p>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label={t_i18n("Close")}>
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        {isHandover ? (
          <div className="space-y-1.5">
            <Label>{t_i18n("Handover type")}</Label>
            <Select
              value={
                typeof edge.label === "string" && edge.label
                  ? edge.label
                  : "delegate"
              }
              disabled={readOnly}
              onValueChange={(v) => onChange({ label: v })}
            >
              <SelectTrigger className="content-compact">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {HANDOVER_TYPES.map((value) => (
                  <SelectItem key={value} value={value}>
                    {t_i18n(HANDOVER_TYPE_CONFIG[value].label)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : (
          <div className="space-y-1.5">
            <Label htmlFor="edge-label">
              {t_i18n("Label")}
            </Label>
            <Input aria-label={t_i18n("Label")}
              id="edge-label"
              value={typeof edge.label === "string" ? edge.label : ""}
              placeholder={t_i18n("Shown along the connection")}
              disabled={readOnly}
              onChange={(e) => onChange({ label: e.target.value || undefined })}
            />
          </div>
        )}

        <div className="space-y-1.5">
          <Label>{t_i18n("Shape")}</Label>
          <Select
            value={edge.data?.pathStyle ?? "bezier"}
            disabled={readOnly}
            onValueChange={(v) =>
              onChange({
                data: {
                  ...(edge.data ?? { kind: "data" }),
                  pathStyle:
                    v === "bezier"
                      ? undefined
                      : (v as "straight" | "step" | "smoothstep"),
                },
              })
            }
          >
            <SelectTrigger className="content-compact">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PATH_STYLES.map((style) => (
                <SelectItem key={style.value} value={style.value}>
                  {t_i18n(style.label)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="border-t border-elevation-subtle p-3">
        <Button
          variant="outline"
          size="sm"
          className="w-full gap-1.5 text-feedback-error-primary hover:text-feedback-error-primary"
          disabled={readOnly}
          onClick={onDelete}
        >
          <Trash2 className="h-3.5 w-3.5" />
          {t_i18n("Delete edge")}
        </Button>
      </div>
    </aside>
  );
}
