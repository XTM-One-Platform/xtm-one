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
 * A distribution as a list (`listStyle`, `viz/rows.ts`): XTM One's bars, a
 * bar under each label; or rows - 50 px, a divider, the item's icon in its
 * colour, its label with ellipsis, its value in SI form, 18 px, weight 600,
 * in the accent - each opening the item's platform link when it has one,
 * the others not hoverable.
 */
import { DeckIcon } from "@/components/presentation/DeckIcon";
import { useFormatter } from "@/i18n";
import { cn, formatCompactNumber } from "@/lib/utils";
import type { WidgetDistributionData } from "@/types";

import { readDistributionStyle } from "../viz/rows";
import { resolveColor } from "./colors";
import { clickable, useChartEvents } from "./common";
import { formatNamedNumber } from "./formats";
import { dataHref } from "./links";

type Item = WidgetDistributionData["items"][number];

function Bars({ data }: { data: WidgetDistributionData }) {
  const max = Math.max(1, ...data.items.map((i) => i.value));
  const { onSelect } = useChartEvents();
  return (
    <ol className="flex flex-col gap-2" data-distribution-style="bars">
      {data.items.map((item, index) => (
        <li
          key={index}
          {...clickable(onSelect, { name: item.label, value: item.label, y: String(item.value) })}
          className={cn("flex flex-col gap-1", onSelect && "cursor-pointer")}
        >
          <span className="flex items-baseline justify-between gap-3 content-compact">
            <span className="min-w-0 truncate text-default-primary">{item.label}</span>
            <span className="shrink-0 tabular-nums text-default-secondary">{formatCompactNumber(item.value)}</span>
          </span>
          <span className="h-1.5 rounded-full bg-elevation-hover">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${(item.value / max) * 100}%` }} />
          </span>
        </li>
      ))}
    </ol>
  );
}

function ItemRow({ item }: { item: Item }) {
  const { locale } = useFormatter();
  const { onSelect } = useChartEvents();
  const href = dataHref(item.link);
  const content = (
    <>
      <span className="flex w-14 shrink-0" style={{ color: resolveColor(item.color) }} data-item-icon={item.icon ?? ""}>
        <DeckIcon name={item.icon} size={24} />
      </span>
      <span className="min-w-0 flex-1 truncate pr-2.5 content-compact text-default-primary">{item.label}</span>
      <span className="mr-5 shrink-0 text-lg font-semibold tabular-nums text-accent">{formatNamedNumber(item.value, "si", locale)}</span>
    </>
  );
  const row = "flex h-[50px] items-center border-b border-elevation-subtle pl-4";
  if (onSelect) {
    const click = clickable(onSelect, { name: item.label, value: item.label, y: String(item.value) });
    return (
      <li {...click} className={cn(row, "hover:bg-elevation-hover", click.className)}>
        {content}
      </li>
    );
  }
  return (
    <li>
      {href ? (
        <a href={href} target="_blank" rel="noopener noreferrer" className={cn(row, "hover:bg-elevation-hover")}>
          {content}
        </a>
      ) : (
        <span className={cn(row, "cursor-default")}>{content}</span>
      )}
    </li>
  );
}

export default function DistributionList({ data, options = {} }: { data: WidgetDistributionData; options?: Record<string, unknown> }) {
  if (readDistributionStyle(options) === "bars") return <Bars data={data} />;
  return (
    <ul className="flex flex-col" data-distribution-style="rows">
      {data.items.map((item, index) => (
        <ItemRow key={index} item={item} />
      ))}
    </ul>
  );
}
