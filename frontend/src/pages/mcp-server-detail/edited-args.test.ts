/**
 * Unit tests for the arguments an MCP server edit sends - `yarn test:unit`.
 *
 * The API shows the arguments masked: a save that did not touch the field must
 * not send them back, or every token would be replaced with its mask.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { argsFieldText, editedArgs, splitArgs } from "./edited-args.ts";

const MASKED = ["-y", "@modelcontextprotocol/server-github", "--token", "ghp_aa***"];

test("an untouched field sends no arguments", () => {
  assert.equal(editedArgs(argsFieldText(MASKED), MASKED), undefined);
});

test("surrounding whitespace alone is not an edit", () => {
  assert.equal(editedArgs(`  ${argsFieldText(MASKED)}\n`, MASKED), undefined);
});

test("an argument holding a space is not split by an unrelated save", () => {
  const args = ["--header", "Authorization: Bearer ***"];
  assert.equal(editedArgs(argsFieldText(args), args), undefined);
});

test("an argument holding a space stays one argument once the field is edited", () => {
  const args = ["--header", "Authorization: Bearer ***"];
  assert.equal(argsFieldText(args), '--header "Authorization: Bearer ***"');
  assert.deepEqual(editedArgs(`${argsFieldText(args)} --verbose`, args), [
    "--header",
    "Authorization: Bearer ***",
    "--verbose",
  ]);
});

test("every argument survives the field text and back", () => {
  const args = ["plain", "two words", 'say "hi"', "it's", "C:\\dir\\x", "", "tab\there", "--token", "***"];
  assert.deepEqual(splitArgs(argsFieldText(args)), args);
});

test("the field is read the way a shell reads a command line", () => {
  assert.deepEqual(splitArgs("  -y   srv  "), ["-y", "srv"]);
  assert.deepEqual(splitArgs(`--msg="hello world" 'a b' x"y z"`), ["--msg=hello world", "a b", "xy z"]);
  // Inside double quotes a backslash-quote is a quote; a lone backslash is itself.
  assert.deepEqual(splitArgs(`"a \\ b \\"c"`), ['a \\ b "c']);
  assert.deepEqual(splitArgs(`"" ''`), ["", ""]);
  // An unclosed quote keeps the rest of the line in one argument.
  assert.deepEqual(splitArgs(`-y "open ended`), ["-y", "open ended"]);
  // A backslash outside quotes is an ordinary character (Windows paths).
  assert.deepEqual(splitArgs("C:\\dir\\x"), ["C:\\dir\\x"]);
});

test("an edited field sends the arguments as typed", () => {
  assert.deepEqual(
    editedArgs("-y @modelcontextprotocol/server-github --token  ghp_new", MASKED),
    ["-y", "@modelcontextprotocol/server-github", "--token", "ghp_new"],
  );
});

test("arguments typed for a server that had none are sent", () => {
  assert.deepEqual(editedArgs("-y server", null), ["-y", "server"]);
  assert.deepEqual(editedArgs("-y server", undefined), ["-y", "server"]);
});

test("a blank field sends no arguments", () => {
  assert.equal(editedArgs("   ", MASKED), undefined);
  assert.equal(editedArgs("", null), undefined);
});
