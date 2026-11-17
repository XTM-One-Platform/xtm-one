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
 * The chart block of a chat message: a fenced `xtm-chart` block of
 * JSON the backend validated (`services/chat_charts.py`, the presentation
 * `chart` contract) and placed in the answer. Pure helpers - parsing, the rows
 * recharts reads, value formatting - so `ChatChart` stays a renderer and the
 * rules are tested in node.
 */

export const CHART_FENCE_LANGUAGE = "xtm-chart";

type ChatChartKind = "bar" | "stacked_bar" | "line" | "area" | "pie" | "donut";

interface ChatChartSeries {
  name: string;
  values: number[];
}

export interface ChatChartBlock {
  type: "chart";
  kind: ChatChartKind;
  categories: string[];
  series: ChatChartSeries[];
  unit?: string;
  format?: "number" | "currency" | "percent";
  decimals?: number;
  title?: string;
  caption?: string;
  stacked?: boolean;
}

const KINDS: readonly ChatChartKind[] = ["bar", "stacked_bar", "line", "area", "pie", "donut"];

/** The block a fence holds, or `null` when it is not one the chat draws (shown as code then). */
export function parseChartBlock(source: string): ChatChartBlock | null {
  let raw: unknown;
  try {
    raw = JSON.parse(source);
  } catch {
    return null;
  }
  if (!raw || typeof raw !== "object") return null;
  const block = raw as Partial<ChatChartBlock>;
  if (block.type !== "chart" || !KINDS.includes(block.kind as ChatChartKind)) return null;
  const categories = block.categories;
  const series = block.series;
  if (!Array.isArray(categories) || !Array.isArray(series) || categories.length === 0 || series.length === 0) return null;
  const valid = series.every(
    (item) =>
      item &&
      typeof item.name === "string" &&
      Array.isArray(item.values) &&
      item.values.length === categories.length &&
      item.values.every((value) => typeof value === "number" && Number.isFinite(value)),
  );
  return valid ? (block as ChatChartBlock) : null;
}

/** One object per category, keyed by series name: what recharts plots. */
export function chartRows(block: ChatChartBlock): Record<string, string | number>[] {
  return block.categories.map((category, index) => {
    const row: Record<string, string | number> = { category };
    for (const item of block.series) row[item.name] = item.values[index];
    return row;
  });
}

/** One slice per category of a pie or donut's one series. */
export function pieRows(block: ChatChartBlock): { name: string; value: number }[] {
  const values = block.series[0]?.values ?? [];
  return block.categories.map((name, index) => ({ name, value: values[index] ?? 0 }));
}

const CURRENCY_SIGNS: Record<string, string> = { USD: "$", EUR: "€", GBP: "£", JPY: "¥" };

/** A value as the chart writes it: `$1.50`, `86%`, `1,204 traces`. */
export function formatChartValue(value: number, block: ChatChartBlock, locale?: string): string {
  const decimals = block.decimals ?? (Number.isInteger(value) ? 0 : 2);
  const amount = value.toLocaleString(locale, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  const unit = (block.unit ?? "").trim();
  if (block.format === "percent") return `${amount}%`;
  if (block.format === "currency") {
    const code = unit || "EUR";
    const sign = CURRENCY_SIGNS[code.toUpperCase()];
    return sign ? `${sign}${amount}` : `${code} ${amount}`;
  }
  if (!unit) return amount;
  return unit.length === 1 ? `${amount}${unit}` : `${amount} ${unit}`;
}
