/**
 * The one tooltip look of the analytics charts: compact rows so the tallest tooltip of a
 * stacked chart (six series and Other) fits a 260 to 300 px plot, the elevation surface and border
 * tokens, and no slide between bars, so a tooltip never travels across the plot's edge. Spread it on
 * every Recharts `<Tooltip>` of a chart page: `<Tooltip {...CHART_TOOLTIP_PROPS} formatter={...} />`.
 */
export const CHART_TOOLTIP_PROPS = {
  contentStyle: {
    backgroundColor: "var(--bg-elevation-default)",
    border: "1px solid var(--border-elevation-subtle)",
    borderRadius: "var(--radius-sm)",
    padding: "6px 8px",
    fontSize: 12,
    lineHeight: "16px",
  },
  labelStyle: { marginBottom: 2, fontWeight: 600 },
  itemStyle: { paddingTop: 1, paddingBottom: 1 },
  isAnimationActive: false,
} as const;
