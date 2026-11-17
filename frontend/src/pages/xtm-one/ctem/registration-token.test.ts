/**
 * Unit tests for the token the connect instructions embed - `yarn test:unit`.
 *
 * The published default must never be presented as the value to paste into a
 * platform: it authenticates nobody and XTM One refuses it for a new platform.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { REGISTRATION_TOKEN_PLACEHOLDER, connectSnippetToken } from "./registration-token.ts";

const COMMAND = 'python -c "import secrets; print(secrets.token_urlsafe(32))"';

test("a token of the operator's own is embedded", () => {
  assert.equal(
    connectSnippetToken({ registration_token: "s3cret", state: "configured", generate_command: COMMAND }),
    "s3cret",
  );
});

test("the published default is never embedded", () => {
  assert.equal(
    connectSnippetToken({
      registration_token: "xtm-default-registration-token",
      state: "published_default",
      generate_command: COMMAND,
    }),
    REGISTRATION_TOKEN_PLACEHOLDER,
  );
});

test("an unset token, a load in progress and a non-admin get the placeholder", () => {
  assert.equal(connectSnippetToken({ registration_token: "", state: "unset", generate_command: COMMAND }), REGISTRATION_TOKEN_PLACEHOLDER);
  assert.equal(connectSnippetToken(undefined), REGISTRATION_TOKEN_PLACEHOLDER);
});
