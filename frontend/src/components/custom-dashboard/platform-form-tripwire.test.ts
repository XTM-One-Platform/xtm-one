/**
 * The platform source form is one engine for every platform: product
 * knowledge lives in the server adapters (`dashboard_platform/<platform>.py`),
 * which describe each product's widget editor. Its files name no platform and
 * none of a product's own configuration keys, so a product branch cannot creep
 * back in - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ENGINE = ["platform-form.ts", "PlatformForm.tsx", "PlatformSourceEditor.tsx", "FilterGroupEditor.tsx", "platform-fields.tsx", "platform-native.ts"];

/** A platform's name, or a key only one product's configuration has. */
const PRODUCT = /open\s*(cti|aev|crq)|dataSelection|widget_config|widget_type|base_entity|regardingOf|"(entities|relationships|audits)"|"structural-histogram"/i;

test("the form engine names no platform and reads no product's keys", () => {
  const found = ENGINE.flatMap((file) =>
    readFileSync(new URL(`./${file}`, import.meta.url), "utf8")
      .split("\n")
      .map((line, index) => ({ file, line: index + 1, text: line.trim() }))
      .filter(({ text }) => PRODUCT.test(text)),
  );
  assert.deepEqual(found, []);
});
