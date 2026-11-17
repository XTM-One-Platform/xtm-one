// "Paste a list" and CSV import of the organization profile lists (#12256 slice 2): separators, header, dedupe, errors.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { PASTE_ERRORS, type PasteSpec, parsePaste, pastedValues, splitLine } from "./organization-list-paste.ts";

type Tech = { product: string; vendor: string; cpe: string };
const TECH: PasteSpec<Tech> = {
  fields: ["product", "vendor", "cpe"],
  empty: { product: "", vendor: "", cpe: "" },
  name: "product",
  headers: { cpe: ["CPE (optional)"] },
  identity: (value) => (value.product.trim() ? `${value.product.trim()}\u0000${value.vendor.trim()}` : ""),
};

type Ai = { name: string; kind: string; owner: string; exposure: string; openaev_asset_id: string };
const AI: PasteSpec<Ai> = {
  fields: ["name", "kind", "exposure", "owner"],
  empty: { name: "", kind: "assistant", owner: "", exposure: "internal", openaev_asset_id: "" },
  name: "name",
  headers: { name: ["AI system"] },
  choices: {
    kind: { assistant: ["Assistant"], agent: ["Agent", "Agent (IA)"], model_api: ["Model API"], embedded: ["Embedded"] },
    exposure: { internal: ["Internal", "Interne"], partner: ["Partner"], public: ["Public"] },
  },
  identity: (value) => value.name.trim(),
};

describe("organization list paste (#12256 slice 2)", () => {
  it("splits a line on a tab, a comma with CSV quotes, a semicolon or a spaced dash", () => {
    assert.deepEqual(splitLine("Jira\tAtlassian"), ["Jira", "Atlassian"]);
    assert.deepEqual(splitLine("Jira, Atlassian"), ["Jira", "Atlassian"]);
    assert.deepEqual(splitLine('"Atlassian, Inc", Jira'), ["Atlassian, Inc", "Jira"]);
    assert.deepEqual(splitLine('"say ""hi""",x'), ['say "hi"', "x"]);
    assert.deepEqual(splitLine("Jira; Atlassian"), ["Jira", "Atlassian"]);
    assert.deepEqual(splitLine("Jira - Atlassian"), ["Jira", "Atlassian"]);
    assert.deepEqual(splitLine("Single-Sign-On"), ["Single-Sign-On"]);
    // A cell the Export protected against formula injection reads back as typed.
    assert.deepEqual(splitLine("'=cmd,x"), ["=cmd", "x"]);
  });

  it("reads lines in the field order, skips blank lines and numbers them as pasted", () => {
    const lines = parsePaste("Jira, Atlassian\n\nSlack\tSalesforce\r\nConcord", TECH, []);
    assert.deepEqual(
      lines.map((line) => [line.line, line.status, line.value?.product, line.value?.vendor]),
      [
        [1, "new", "Jira", "Atlassian"],
        [3, "new", "Slack", "Salesforce"],
        [4, "new", "Concord", ""],
      ],
    );
  });

  it("flags a line already in the list or earlier in the paste as a duplicate, case and spaces aside", () => {
    const lines = parsePaste("jira , atlassian\nNotion, Notion Labs\nnotion,notion labs", TECH, [{ product: "Jira", vendor: "Atlassian", cpe: "" }]);
    assert.deepEqual(lines.map((line) => line.status), ["duplicate", "new", "duplicate"]);
    assert.deepEqual(pastedValues(lines).map((value) => value.product), ["Notion"]);
  });

  it("refuses a line with no name", () => {
    const lines = parsePaste(", Atlassian", TECH, []);
    assert.equal(lines[0].status, "error");
    assert.equal(lines[0].error, PASTE_ERRORS.noName);
  });

  it("maps the columns of the CSV Export by its header, in any order and with the header's words", () => {
    const csv = "\ufeffVendor,Product,CPE (optional)\r\nAtlassian,Jira,cpe:/a:atlassian:jira\r\n";
    const lines = parsePaste(csv, TECH, []);
    assert.equal(lines.length, 1);
    assert.deepEqual(lines[0].value, { product: "Jira", vendor: "Atlassian", cpe: "cpe:/a:atlassian:jira" });
  });

  it("reads a choice by its value or its label, keeps the default on an empty cell, refuses an unknown one", () => {
    const lines = parsePaste("Copilot, Agent, Public, Sam\nBot,,,\nOdd, robot", AI, []);
    assert.deepEqual(lines[0].value, { name: "Copilot", kind: "agent", exposure: "public", owner: "Sam", openaev_asset_id: "" });
    assert.deepEqual(lines[1].value, { name: "Bot", kind: "assistant", exposure: "internal", owner: "", openaev_asset_id: "" });
    assert.equal(lines[2].status, "error");
    assert.equal(lines[2].error, PASTE_ERRORS.badChoice);
    assert.equal(lines[2].field, "kind");
    // The CSV Export writes the stored values under the translated headers.
    const csv = parsePaste("AI system,Kind,Exposure,Owner\nX,model_api,partner,", AI, []);
    assert.equal(csv[0].value?.kind, "model_api");
    assert.equal(csv[0].value?.exposure, "partner");
  });

  it("does not take a first line for a header unless it names the row's name field", () => {
    const lines = parsePaste("Vendor\nJira", TECH, []);
    assert.deepEqual(lines.map((line) => line.value?.product), ["Vendor", "Jira"]);
  });
});
