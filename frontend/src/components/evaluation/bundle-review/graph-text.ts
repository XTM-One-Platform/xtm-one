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

// What a node and an edge of the two graphs say: a node's name and second line, an edge's label.

import { EXPECTED, nearKind, type EntityItem, type RelationshipItem } from "@/lib/evaluation-bundle-review";
import { NEAR_KIND_LABELS, statusLabel } from "./labels";
import type { ReviewContext } from "./context";

type Translate = (id: string, values?: Record<string, string | number>) => string;

export function nodeName(it: EntityItem, side: "a" | "b" | "both" = "both"): string {
  const fallback = it.g.by[EXPECTED] ?? Object.values(it.g.all)[0][0];
  return (side === "b" ? it.b ?? it.a ?? fallback : it.a ?? it.b ?? fallback).name;
}

/** The second line of a node: the other name of a near-miss, or where the object is missing. */
export function nodeSub(ctx: ReviewContext, it: EntityItem, t_i18n: Translate, side: "a" | "b" | "both" = "both"): string {
  if (it.a && it.b && it.a.cid !== it.b.cid) return `≈ ${side === "b" ? it.a.name : it.b.name}`;
  if (ctx.mode === "alone") {
    const exp = it.g.by[EXPECTED];
    if (it.st === "near" && exp && it.b) return t_i18n("≈ expected: {name}", { name: exp.name }) + ` (${t_i18n(NEAR_KIND_LABELS[nearKind(exp, it.b)])})`;
    return it.st === "fp" ? t_i18n("not expected") : "";
  }
  if (ctx.mode === "runs") {
    if (!it.b) return t_i18n("only in {source}", { source: ctx.letter(ctx.pair.ref) });
    if (!it.a) return t_i18n("only in {source}", { source: ctx.letter(ctx.pair.under) });
    return it.st !== "same" ? t_i18n(statusLabel(it.st, "runs")) : "";
  }
  return it.st === "fn" ? t_i18n("missing") : it.st === "fp" ? t_i18n("spurious") : "";
}

/** An edge's label: the type, or both types when the two sides disagree. */
export function edgeLabel(ctx: ReviewContext, it: RelationshipItem, side: "a" | "b" | "both"): string {
  const { r } = it;
  const { ref: A, under: B } = ctx.pair;
  if (side === "a") return r.kinds[A] ?? "";
  if (side === "b") return r.kinds[B] ?? "";
  if (it.inA && it.inB && r.kinds[A] !== r.kinds[B]) return `${r.kinds[A]} ≠ ${r.kinds[B]}`;
  return (it.inB ? r.kinds[B] : r.kinds[A]) ?? "";
}
