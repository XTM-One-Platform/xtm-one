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
 * The type of an upstream value, and where the builder learned it:
 * a declared structure, the last run, or both.
 */

import { useFormatter } from "@/i18n";
import { cn } from "@/lib/utils";
import type { ShapeType } from "./output-shapes";

/** @i18n-keys */
const TYPE_LABELS = {
  text: "Text",
  number: "Number",
  boolean: "Boolean",
  object: "Object",
  list: "List",
  null: "Empty",
  any: "Any value",
} as const satisfies Record<ShapeType, string>;

export function ShapeTypeBadge({
  type,
  declared,
  observed,
  className,
}: {
  type?: ShapeType;
  declared?: boolean;
  observed?: boolean;
  className?: string;
}) {
  const { t_i18n } = useFormatter();
  if (!type && !declared && !observed) return null;
  const origin =
    declared && observed
      ? t_i18n("Declared, seen in the last run")
      : declared
        ? t_i18n("Declared")
        : observed
          ? t_i18n("Seen in the last run")
          : undefined;
  return (
    <span
      title={origin}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-sm border border-elevation-subtle px-1 font-mono-plex text-[9px] leading-4 text-default-secondary",
        className,
      )}
    >
      {type && t_i18n(TYPE_LABELS[type])}
      {declared && <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-accent" />}
      {observed && <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-feedback-success-primary" />}
    </span>
  );
}
