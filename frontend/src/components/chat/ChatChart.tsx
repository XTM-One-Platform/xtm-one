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
 * A chart in a chat answer: the `xtm-chart` block `render_chart`
 * validated, drawn with the LLM Analytics charts' primitives (recharts, the
 * evaluation palette, the elevation tokens). Loaded on demand by
 * `ChatMarkdown`, so recharts stays out of the chat's first chunk.
 */
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card, CardContent } from "@/components/ui/card";
import { CHART_TOOLTIP_PROPS } from "@/components/ui/charts/chart-tooltip";
import { EVALUATION_CHART_COLORS } from "@/lib/constants";
import { useFormatter } from "@/i18n";
import { chartRows, formatChartValue, pieRows, type ChatChartBlock } from "@/components/chat/chat-chart";

const AXIS_TICK = { fill: "var(--text-default-secondary)", fontSize: 11 };
const AXIS_STROKE = "var(--border-elevation-subtle)";

function color(index: number): string {
  return EVALUATION_CHART_COLORS[index % EVALUATION_CHART_COLORS.length];
}

export default function ChatChart({ block }: { block: ChatChartBlock }) {
  const { locale } = useFormatter();
  const format = (value: unknown) => formatChartValue(Number(value), block, locale);
  const multi = block.series.length > 1;
  const stacked = block.kind === "stacked_bar" || block.stacked === true;
  const rows = chartRows(block);

  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" stroke={AXIS_STROKE} opacity={0.4} />
      <XAxis dataKey="category" tick={AXIS_TICK} stroke={AXIS_STROKE} />
      <YAxis tickFormatter={format} tick={AXIS_TICK} stroke={AXIS_STROKE} width={72} />
      <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(value, key) => [format(value), String(key)]} />
      {multi && <Legend wrapperStyle={{ fontSize: 11 }} />}
    </>
  );

  let chart: React.ReactElement;
  if (block.kind === "pie" || block.kind === "donut") {
    chart = (
      <PieChart>
        <Pie
          data={pieRows(block)}
          dataKey="value"
          nameKey="name"
          innerRadius={block.kind === "donut" ? "55%" : 0}
          outerRadius="80%"
          label={({ value }) => format(value)}
        >
          {block.categories.map((category, index) => (
            <Cell key={category} fill={color(index)} />
          ))}
        </Pie>
        <Tooltip {...CHART_TOOLTIP_PROPS} formatter={(value, key) => [format(value), String(key)]} />
        <Legend wrapperStyle={{ fontSize: 11 }} />
      </PieChart>
    );
  } else if (block.kind === "line") {
    chart = (
      <LineChart data={rows} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
        {axes}
        {block.series.map((item, index) => (
          <Line key={item.name} type="monotone" dataKey={item.name} stroke={color(index)} strokeWidth={2} dot={rows.length <= 16} />
        ))}
      </LineChart>
    );
  } else if (block.kind === "area") {
    chart = (
      <AreaChart data={rows} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
        {axes}
        {block.series.map((item, index) => (
          <Area
            key={item.name}
            type="monotone"
            dataKey={item.name}
            stackId={stacked ? "stack" : undefined}
            stroke={color(index)}
            fill={color(index)}
            fillOpacity={0.3}
          />
        ))}
      </AreaChart>
    );
  } else {
    chart = (
      <BarChart data={rows} margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
        {axes}
        {block.series.map((item, index) => (
          <Bar key={item.name} dataKey={item.name} stackId={stacked ? "stack" : undefined} fill={color(index)} radius={stacked ? 0 : [4, 4, 0, 0]} />
        ))}
      </BarChart>
    );
  }

  return (
    <figure className="my-4" aria-label={block.title ?? undefined}>
      <Card>
        <CardContent className="p-4">
          {block.title && <p className="content-base font-medium mb-3">{block.title}</p>}
          <div className="h-[280px]" role="img" aria-label={block.title ?? block.series.map((item) => item.name).join(", ")}>
            <ResponsiveContainer width="100%" height="100%">
              {chart}
            </ResponsiveContainer>
          </div>
          {block.caption && (
            <figcaption className="content-caption text-default-secondary mt-3">{block.caption}</figcaption>
          )}
        </CardContent>
      </Card>
    </figure>
  );
}
