/**
 * A video template's rules as the template page shows them - `yarn test:unit`.
 *
 * The art direction of its AI shots is read from `extra_metadata.video.shots`:
 * the look and what no shot shows; a template that declares none shows none.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { renderGateReasonText, rulesFromMetadata } from "./video-format.ts";

/** A stand-in for `t_i18n`: fills `{name}` and one `{count, plural, one {...} other {...}}` the way ICU does. */
function t(key: string, values: Record<string, string | number> = {}): string {
  return key
    .replace(/\{(\w+), plural, one \{([^}]*)\} other \{([^}]*)\}\}/g, (_all, name: string, one: string, other: string) =>
      (Number(values[name]) === 1 ? one : other).replace("#", String(values[name])),
    )
    .replace(/\{(\w+)\}/g, (_all, name: string) => String(values[name] ?? `{${name}}`));
}

const format = { timeAgo: (iso: string) => `ago(${iso})`, dateTime: (iso: string) => `at(${iso})`, locale: "en" };
const ja = { ...format, locale: "ja" };
const RENDER_ID = "94909d69-b2f3-4a6e-bf25-3f607ed4b026";
const message = `render \`${RENDER_ID}\` (draft) of this same revision left 28 finding(s) open (duplicate_logo at 0:00.00)`;

test("a template's art direction is read from its rules", () => {
  const rules = rulesFromMetadata({
    video: { shots: { style: "Filmed in natural light.", avoid: ["laser beams", 3, "starfields"] } },
  });
  assert.deepEqual(rules?.shots, { style: "Filmed in natural light.", avoid: ["laser beams", "starfields"] });
});

test("a refused render's open findings read without its id or a kind key, with a real plural", () => {
  const many = renderGateReasonText(
    {
      code: "open_findings",
      message,
      params: {
        count: 28,
        quality: "draft",
        created_at: "2026-11-07T12:00:00+00:00",
        findings: [
          { kind: "duplicate_logo", at: 0 },
          { kind: "insert_alignment", at: 1 },
          { kind: "not_a_kind_yet", at: null },
        ],
      },
    },
    t,
    format,
  );
  assert.ok(!many.includes(RENDER_ID) && !/[a-z]+_[a-z]+/.test(many), many);
  assert.match(many, /^A render of this revision \(Draft \(720p, 30 fps\), ago\(2026-11-07T12:00:00\+00:00\), at\(2026-11-07T12:00:00\+00:00\)\) left 28 findings open: /);
  assert.match(many, /Brand mark seen twice in one frame at 0:00\.00, Screen insert off its surface at 0:01\.00, another finding, and 25 more\. Change what they point at, or preview their scenes again:/);
  const one = renderGateReasonText(
    { code: "open_findings", message, params: { count: 1, quality: "high", created_at: null, findings: [{ kind: "stutter", at: 3 }] } },
    t,
    format,
  );
  assert.match(one, /^A render of this revision \(High \(1080p, 60 fps\)\) left 1 finding open: Stutter at 0:03\.00\. Change what it points at, or preview its scene again: /);
});

test("a refused render's findings are joined the way the reader's language joins a list", () => {
  const text = renderGateReasonText(
    {
      code: "open_findings",
      message,
      params: { count: 3, quality: "draft", created_at: null, findings: [{ kind: "stutter", at: 1 }, { kind: "stutter", at: 2 }] },
    },
    t,
    ja,
  );
  assert.match(text, /open: Stutter at 0:01\.00、Stutter at 0:02\.00、1 more\. /);
});

test("a gate reason the editor cannot build shows the backend's message", () => {
  assert.equal(renderGateReasonText({ code: "open_findings", message }, t, format), message);
  assert.equal(renderGateReasonText({ code: "scene_changed", message: "scene a changed", params: { count: 2 } }, t, format), "scene a changed");
});

test("a template without an art direction shows none", () => {
  assert.equal(rulesFromMetadata({ video: { aspect_ratios: ["16:9"] } })?.shots, null);
  assert.deepEqual(rulesFromMetadata({ video: { shots: { style: 7 } } })?.shots, { style: undefined, avoid: [] });
});
