/**
 * Unit tests for an agent's email signature overrides - `yarn test:unit`.
 *
 * One binding changes at a time; the others, seen or not, are kept as they
 * are, and a binding that only inherits keeps no entry.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { nextIntegrationSignatures, signatureOptions } from "./email-signatures.ts";

test("a picker keeps a chosen signature integration the viewer cannot choose", () => {
  const available = [{ id: "a", name: "Signatures", provider: "letsignit" }];
  assert.deepEqual(signatureOptions(available, "a", "Signatures", "Unavailable"), [{ value: "a", label: "Signatures" }]);
  assert.deepEqual(signatureOptions(available, "b", null, "Unavailable"), [
    { value: "a", label: "Signatures" },
    { value: "b", label: "Unavailable" },
  ]);
});

test("a mode change keeps the other bindings' overrides", () => {
  const current = { other: { signature_mode: "on" as const } };
  assert.deepEqual(nextIntegrationSignatures(current, "mail", { signature_mode: "off" }), {
    other: { signature_mode: "on" },
    mail: { signature_mode: "off" },
  });
});

test("a chosen signature integration survives a mode change and goes back to inherited on null", () => {
  const chosen = nextIntegrationSignatures(null, "mail", { signature_integration_id: "sig" });
  assert.deepEqual(chosen, { mail: { signature_mode: "inherit", signature_integration_id: "sig" } });
  const on = nextIntegrationSignatures(chosen, "mail", { signature_mode: "on" });
  assert.deepEqual(on, { mail: { signature_mode: "on", signature_integration_id: "sig" } });
  assert.deepEqual(nextIntegrationSignatures(on, "mail", { signature_integration_id: null }), {
    mail: { signature_mode: "on" },
  });
});

test("a binding that only inherits keeps no entry, and none left is null", () => {
  const current = { mail: { signature_mode: "off" as const } };
  assert.equal(nextIntegrationSignatures(current, "mail", { signature_mode: "inherit" }), null);
});
