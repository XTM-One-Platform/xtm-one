/**
 * Unit tests for the pack options the picker confirms - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { SourcePackOption } from "../../types/index.ts";
import { choiceHint, confirmedPackOptions, defaultPackOptions } from "./pack-options.ts";

const pack = (slug: string, withOption: boolean): SourcePackOption => ({
  slug,
  label: slug,
  description: "",
  subject_types: [],
  recommended: false,
  applicable_source_count: 1,
  total_source_count: 1,
  applicable_source_names: [],
  max_iterations: 10,
  options: withOption
    ? [
        {
          key: "ip_pivot",
          label: "Pivot depth",
          description: "",
          default: "shared",
          choices: [
            { value: "shared", label: "Shared IPs only", description: "" },
            { value: "all", label: "Every IP", description: "" },
          ],
        },
      ]
    : undefined,
});

const PACKS = [pack("disinfo", true), pack("web", false)];

test("each option starts on its default", () => {
  assert.deepEqual(defaultPackOptions(PACKS), { disinfo: { ip_pivot: "shared" } });
});

test("a confirmed pack sends its choice, its default included", () => {
  assert.deepEqual(confirmedPackOptions(PACKS, ["disinfo", "web"], { disinfo: { ip_pivot: "all" } }), {
    disinfo: { ip_pivot: "all" },
  });
  assert.deepEqual(confirmedPackOptions(PACKS, ["disinfo"], {}), { disinfo: { ip_pivot: "shared" } });
});

test("an unticked pack sends nothing, and an unknown value falls back to the default", () => {
  assert.deepEqual(confirmedPackOptions(PACKS, ["web"], { disinfo: { ip_pivot: "all" } }), {});
  assert.deepEqual(confirmedPackOptions(PACKS, ["disinfo"], { disinfo: { ip_pivot: "gone" } }), {
    disinfo: { ip_pivot: "shared" },
  });
});

test("a choice without a description shows no hint, rather than an empty catalog key", () => {
  const option = {
    key: "step.enabled",
    label: "A step",
    description: "",
    default: "on",
    choices: [
      { value: "on", label: "On", description: "" },
      { value: "off", label: "Off", description: "Skipped" },
    ],
  };
  assert.equal(choiceHint(option, "on"), null);
  assert.equal(choiceHint(option, "off"), "Skipped");
  assert.equal(choiceHint(option, "gone"), null);
});
