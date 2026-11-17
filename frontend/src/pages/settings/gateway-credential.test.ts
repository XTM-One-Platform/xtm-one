/** Unit tests for the gateway credential the Add Provider form builds - `yarn test:unit`. */

import { test } from "node:test";
import assert from "node:assert/strict";

import { buildGatewayCredential } from "./gateway-credential.ts";

test("the credential is the bare URL without a token", () => {
  assert.equal(buildGatewayCredential(" https://gw.example.com ", ""), "https://gw.example.com");
  assert.equal(buildGatewayCredential("https://gw.example.com", "   "), "https://gw.example.com");
});

test("a token follows an empty target", () => {
  assert.equal(buildGatewayCredential("https://gw.example.com", " tok "), "https://gw.example.com||||tok");
});

test("a token that contains the separator stays whole", () => {
  assert.equal(buildGatewayCredential("https://gw.example.com", "a||b"), "https://gw.example.com||||a||b");
});
