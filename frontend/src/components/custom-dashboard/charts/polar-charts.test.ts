/**
 * Unit tests for the radar, polar area, word cloud and heatmap geometry - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { frameOf } from "./dynamic-options.ts";
import { axisLabelPlaces, heatmapGrid, heatShare, itemsOf, labelPixels, polarRings, polarSectors, radarFit, readHeatmapOptions, readRadarOptions, readWordCloudOptions, wordCloudLayout, wordWidth } from "./polar-charts.ts";

const distribution = { items: [{ label: "APT28", value: 40 }, { label: "Lazarus", value: 10 }, { label: "FIN7", value: 20 }, { label: "Kimsuky", value: 0 }] };
const series = {
  series: [
    { name: "Reports", points: [{ x: "2026-10-01", y: 3 }, { x: "2026-10-08", y: 0 }] },
    { name: "Notes", points: [{ x: "2026-10-08", y: 9 }] },
  ],
};

test("a polar area gives each item an equal angle and a radius that grows with its value", () => {
  const sectors = polarSectors(itemsOf("distribution", frameOf("distribution", distribution)));
  assert.deepEqual(
    sectors.map((s) => [s.label, s.value, s.index]),
    [["APT28", 40, 0], ["Lazarus", 10, 1], ["FIN7", 20, 2], ["Kimsuky", 0, 3]],
  );
  // The largest reaches the rim, from the top clockwise; a quarter of the circle each.
  assert.equal(sectors[0].path, "M 0 0 L 0 -1 A 1 1 0 0 1 1 0 Z");
  assert.equal(sectors[1].path, "M 0 0 L 0.25 0 A 0.25 0.25 0 0 1 0 0.25 Z");
  assert.equal(sectors[3].path, "M 0 0 L 0 0 A 0 0 0 0 1 0 0 Z");
  // One item is a whole circle.
  assert.match(polarSectors([{ label: "only", value: 5 }])[0].path, /^M 0 -1 A 1 1 0 1 1 0 1 A 1 1 0 1 1 0 -1 Z$/);
  assert.deepEqual(polarSectors([]), []);
  // On a scale whose outer ring is past the largest value, the largest stops short of the rim.
  assert.equal(polarSectors([{ label: "a", value: 30 }, { label: "b", value: 15 }], 40)[0].path, "M 0 0 L 0 -0.75 A 0.75 0.75 0 0 1 0 0.75 Z");
});

test("a polar area's rings are round steps of 1, 2, 2.5 or 5 times a power of ten up to the largest value", () => {
  assert.deepEqual(polarRings(40), [10, 20, 30, 40]);
  assert.deepEqual(polarRings(37), [10, 20, 30, 40]);
  assert.deepEqual(polarRings(1520), [500, 1000, 1500, 2000]);
  assert.deepEqual(polarRings(7), [2, 4, 6, 8]);
  assert.deepEqual(polarRings(0.9), [0.2, 0.4, 0.6, 0.8, 1]);
  assert.deepEqual(polarRings(0), []);
});

test("a heatmap has a row per series and a column per x, the gaps kept", () => {
  const grid = heatmapGrid("series", frameOf("series", series));
  assert.deepEqual(grid, {
    rows: ["Reports", "Notes"],
    columns: ["2026-10-01", "2026-10-08"],
    cells: [[3, 0], [null, 9]],
    min: 0,
    max: 9,
  });
  assert.deepEqual(heatmapGrid("distribution", frameOf("distribution", distribution)).cells, [[40, 10, 20, 0]]);
  assert.deepEqual([heatShare(0, 0, 9), heatShare(9, 0, 9), heatShare(3, 0, 9).toFixed(3), heatShare(4, 4, 4)], [0, 1, "0.333", 1]);
});

test("a word cloud places the largest word first, at the centre, sized on a log scale (d3's, truncated to a pixel)", () => {
  const words = wordCloudLayout(itemsOf("distribution", frameOf("distribution", distribution)), 400, 200, 20, 50);
  // ln 20 sits half way between ln 10 and ln 40: 35 px, kept whole.
  assert.deepEqual(words.map((w) => [w.text, w.size]), [["APT28", 50], ["FIN7", 35], ["Lazarus", 20]]);
  // One word alone is half way; the spiral steps 4 px down for 4 x width / height across.
  assert.deepEqual(wordCloudLayout([{ label: "solo", value: 3 }], 400, 200, 20, 50).map((w) => w.size), [35]);
  const spiral = wordCloudLayout([{ label: "AAAAAAAAAA", value: 9 }, { label: "BB", value: 1 }], 400, 200, 20, 50);
  assert.ok(spiral[1].x % 8 === 0 && spiral[1].y % 4 === 0, `${spiral[1].x},${spiral[1].y}`);
  assert.deepEqual([words[0].x, words[0].y], [0, 0]);
  // No two words overlap, and none leaves the cloud.
  for (const a of words) {
    assert.ok(Math.abs(a.x) + a.width / 2 <= 200 && Math.abs(a.y) + a.height / 2 <= 100, a.text);
    for (const b of words) {
      if (a !== b) assert.ok(Math.abs(a.x - b.x) * 2 >= a.width + b.width || Math.abs(a.y - b.y) * 2 >= a.height + b.height, `${a.text}/${b.text}`);
    }
  }
  // The same words, the same places.
  assert.deepEqual(wordCloudLayout(itemsOf("distribution", frameOf("distribution", distribution)), 400, 200, 20, 50), words);
  // A wide cloud reaches its corners: seven words of a small widget all find room.
  const seven = ["IPv4-Addr", "Domain-Name", "Url", "StixFile", "Email-Addr", "Hostname", "IPv6-Addr"].map((label, i) => ({ label, value: 90 - i * 12 }));
  assert.equal(wordCloudLayout(seven, 370, 240, 20, 50).length, 7);
  // A smaller cloud lays them out smaller rather than leave some out.
  const small = wordCloudLayout(seven, 340, 190, 20, 50);
  assert.deepEqual([small.length, Math.max(...small.map((w) => w.size)) < 50], [7, true]);
  // A word wider than the cloud shrinks; one with no room is left out.
  const long = wordCloudLayout([{ label: "a-very-long-intrusion-set-name", value: 5 }], 400, 60, 20, 50);
  assert.deepEqual([long[0].size, wordWidth(long[0].text, long[0].size) <= 400], [23, true]);
  // Narrower still, the cloud is laid out smaller, under its smallest size.
  assert.deepEqual(wordCloudLayout([{ label: "a-very-long-intrusion-set-name", value: 5 }], 300, 60, 20, 50).map((w) => w.size), [17]);
  assert.deepEqual(wordCloudLayout([{ label: "x", value: 1 }], 8, 8, 20, 50), []);
});

test("options are read with OpenCTI's defaults and bounded", () => {
  assert.deepEqual(readRadarOptions({}), { areaOpacity: 0.2, lineWidth: 1, legendDisplay: "bottom", legendTruncation: "ellipsisEnd", seriesColors: [], seriesColorsByField: {} });
  assert.equal(readRadarOptions({ lineWidth: 2 }).lineWidth, 2);
  assert.deepEqual(readWordCloudOptions({ fontSizeMin: 60, fontSizeMax: 14 }), { fontSizeMin: 14, fontSizeMax: 32, seriesColors: [] });
  assert.deepEqual(readHeatmapOptions({ colorMin: "#fff", colorMax: "url(x)", dataValuesDisplay: "all" }), { colorMin: "#fff", colorMax: undefined, dataValuesDisplay: "all", legendDisplay: "bottom" });
});

test("a radar's labels and radius fit its box", () => {
  assert.equal(labelPixels("CVE-2026-21413", 11), 93);
  // Wide enough: OpenCTI's 25 characters, a radius the height allows.
  assert.deepEqual(radarFit(["Phishing", "Valid Accounts", "Exfiltration"], 600, 300, 11, 25), { chars: 25, radius: 127 });
  // Narrow: the shape shrinks to 30 px before the labels are cut; a CVE id (14 characters) stays whole.
  const narrow = radarFit(["Exploit Public-Facing Application", "CVE-2026-21413", "T1"], 270, 380, 11, 25);
  assert.deepEqual(narrow, { chars: 14, radius: 32 });
  assert.ok(narrow.radius + narrow.chars * 11 * 0.6 + 10 <= 135);
  // Never under 4 characters nor a 30 px radius.
  assert.deepEqual(radarFit(["A very long technique name", "B", "C"], 60, 60, 11, 25), { chars: 4, radius: 30 });
  // Two axes put their labels above and below the shape: they keep their 25 characters, and the radius is
  // ApexCharts' (293 / 2.1 - 1 - 25 x 6.6 / 1.75 = 44).
  const pattern = "Spear Phishing Attack Pattern";
  assert.deepEqual(radarFit([pattern, pattern], 293, 384, 11, 25), { chars: 25, radius: 44 });
});

test("an axis writes the labels that have room, kept inside it", () => {
  // Ten columns 20 px apart, labels 50 px wide: the first, pushed inside the axis (25), then each 8 px past the last.
  const centers = [...Array(10).keys()].map((i) => 10 + i * 20);
  const kept = axisLabelPlaces(centers, centers.map(() => 50), 0, 200);
  assert.deepEqual(kept.map((k) => k.index), [0, 4, 7]);
  // The first is moved inside the axis; none overlaps the one before it.
  assert.equal(kept[0].x, 25);
  kept.slice(1).forEach((k, i) => assert.ok(k.x - 25 >= kept[i].x + 25 + 8));
  // The last place, near the end, is kept inside too.
  assert.deepEqual(axisLabelPlaces([190], [50], 0, 200), [{ index: 0, x: 175 }]);
});
