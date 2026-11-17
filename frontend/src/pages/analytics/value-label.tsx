// SPDX-License-Identifier: Apache-2.0

/**
 * The figure written over a bar or a line point of an LLM Analytics chart:
 * flat when the bucket has room, a size smaller, then staggered
 * on two alternating rows, then diagonal (45 degrees), hidden when even that does
 * not fit (the tooltip keeps the value). Never turned 90 degrees. The mode comes
 * from `valueLabelLayout`, the position from `placeValueLabel`: staggered and
 * diagonal figures sit on rows shared by the whole chart, above the tallest bar,
 * so two neighbours of different heights never meet.
 * Passed to a Recharts `LabelList` as its `content`; an empty value draws
 * nothing (a stack segment that is not the top of its stack).
 */
import type { LabelProps } from "recharts";
import { placeValueLabel, type ValueLabelLayout } from "@/pages/analytics/chart-data";

export function drawValueLabel(props: LabelProps, layout: ValueLabelLayout, fill: string) {
  const { viewBox, value, index } = props;
  const text = typeof value === "string" || typeof value === "number" ? String(value) : "";
  if (!text || !viewBox || !("x" in viewBox)) return null;
  const place = placeValueLabel(layout, {
    x: Number(viewBox.x) || 0,
    y: Number(viewBox.y) || 0,
    width: "width" in viewBox ? Number(viewBox.width) || 0 : 0,
    index: Number(index) || 0,
    text,
  });
  if (!place) return null;
  return (
    <text
      x={place.x}
      y={place.y}
      transform={place.angle ? `rotate(${place.angle} ${place.x} ${place.y})` : undefined}
      textAnchor={place.anchor}
      fill={fill}
      fontSize={layout.fontSize}
      className="tabular-nums"
      data-label-mode={layout.mode}
    >
      {text}
    </text>
  );
}
