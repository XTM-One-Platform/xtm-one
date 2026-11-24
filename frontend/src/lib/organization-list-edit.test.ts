// The editing model of the organization profile lists (#12256): row states, the values Save writes, the attention checks.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  addRow,
  addRows,
  attentionCount,
  changeCounts,
  committedValues,
  fieldSuggestions,
  isCpe,
  isRepository,
  LIST_ISSUES,
  LIST_RULES,
  patchRow,
  patchRows,
  removeRow,
  removeRows,
  rowIssues,
  rowSearchFields,
  rowState,
  setRowValue,
  startRows,
  undoRemove,
} from "./organization-list-edit.ts";

type Tech = { product: string; vendor: string; cpe: string };
const tech = (product: string, vendor = "", cpe = ""): Tech => ({ product, vendor, cpe });
const techCheck = (value: Tech) => [
  ...(value.product.trim() ? [] : [LIST_ISSUES.emptyName]),
  ...(value.product.trim() && !value.vendor.trim() ? [LIST_ISSUES.vendor] : []),
  ...(isCpe(value.cpe) ? [] : [LIST_ISSUES.cpe]),
];
const techId = (value: Tech) => (value.product.trim() ? `${value.product}|${value.vendor}` : "");

describe("organization list edit (#12256)", () => {
  it("starts every saved row unchanged, and counts nothing", () => {
    const rows = startRows([tech("Jira", "Atlassian"), tech("Slack", "Salesforce")]);
    assert.deepEqual(rows.map(rowState), ["unchanged", "unchanged"]);
    assert.deepEqual(changeCounts(rows), { added: 0, edited: 0, removed: 0 });
    assert.deepEqual(committedValues(rows), [tech("Jira", "Atlassian"), tech("Slack", "Salesforce")]);
  });

  it("adds a row first, keyed apart from the saved ones, and writes it after them in the order added", () => {
    let rows = startRows([tech("Jira", "Atlassian")]);
    const first = addRow(rows, tech("A"));
    const second = addRow(first.rows, tech("B"));
    rows = second.rows;
    assert.notEqual(first.key, second.key);
    assert.equal(rows[0].key, second.key);
    assert.equal(rowState(rows[0]), "new");
    assert.deepEqual(committedValues(rows).map((row) => row.product), ["Jira", "A", "B"]);
    assert.deepEqual(changeCounts(rows), { added: 2, edited: 0, removed: 0 });
  });

  it("marks an edited row, and unmarks it when typed back to the saved value", () => {
    let rows = startRows([tech("Jira", "")]);
    rows = patchRow(rows, "r0", { vendor: "Atlassian" });
    assert.equal(rowState(rows[0]), "edited");
    // The base, which the table sorts and filters on, does not move while typed in.
    assert.equal(rows[0].base.vendor, "");
    rows = patchRow(rows, "r0", { vendor: "" });
    assert.equal(rowState(rows[0]), "unchanged");
  });

  it("reverts an open row to the value it had when opened (Esc)", () => {
    let rows = startRows([tech("Jira", "Atlassian")]);
    const opened = rows[0].value;
    rows = patchRow(rows, "r0", { product: "Jirra" });
    rows = setRowValue(rows, "r0", opened);
    assert.deepEqual(rows[0].value, tech("Jira", "Atlassian"));
  });

  it("keeps a removed saved row until Save with Undo, and drops an added row at once", () => {
    let rows = startRows([tech("Jira", "Atlassian"), tech("Slack", "Salesforce")]);
    rows = removeRow(rows, "r0");
    assert.equal(rows.length, 2);
    assert.equal(rowState(rows[0]), "removed");
    assert.deepEqual(committedValues(rows).map((row) => row.product), ["Slack"]);
    rows = undoRemove(rows, "r0");
    assert.equal(rowState(rows[0]), "unchanged");
    const added = addRow(rows, tech("New"));
    rows = removeRow(added.rows, added.key);
    assert.equal(rows.length, 2);
    assert.deepEqual(changeCounts(rows), { added: 0, edited: 0, removed: 0 });
  });

  it("flags an empty vendor, an invalid CPE and a duplicate, never a removed row", () => {
    let rows = startRows([tech("Concord", ""), tech("Jira", "Atlassian", "cpe:2.3:a:atlassian:jira:*:*:*:*:*:*:*:*"), tech("jira", "atlassian"), tech("Bad", "X", "not a cpe")]);
    let issues = rowIssues(rows, techCheck, techId);
    assert.deepEqual(issues.get("r0"), [LIST_ISSUES.vendor]);
    assert.deepEqual(issues.get("r1"), [LIST_ISSUES.duplicate]);
    assert.deepEqual(issues.get("r2"), [LIST_ISSUES.duplicate]);
    assert.deepEqual(issues.get("r3"), [LIST_ISSUES.cpe]);
    rows = removeRow(rows, "r2");
    issues = rowIssues(rows, techCheck, techId);
    assert.equal(issues.has("r1"), false);
    assert.equal(issues.has("r2"), false);
    assert.equal(attentionCount([tech("Concord", ""), tech("Jira", "Atlassian")], techCheck, techId), 1);
  });

  it("reads a CPE 2.3 formatted string and the 2.2 URI, empty allowed", () => {
    assert.equal(isCpe(""), true);
    assert.equal(isCpe("cpe:2.3:a:atlassian:jira:9.4:*:*:*:*:*:*:*"), true);
    assert.equal(isCpe("cpe:/a:atlassian:jira:9.4"), true);
    assert.equal(isCpe("cpe:2.3:a:vendor:product:*"), false);
    assert.equal(isCpe("jira"), false);
  });

  it("reads a source repository as owner/name, a GitHub address included, and flags a shipped product row that is not (#1432)", () => {
    assert.equal(isRepository("acme/server"), true);
    assert.equal(isRepository("https://github.com/acme/server.git"), true);
    assert.equal(isRepository("server"), false);
    assert.equal(isRepository(""), false);
    const rules = LIST_RULES.shipped_products;
    assert.deepEqual(rules.check({ product: "Acme Server", vendor: "Acme", cpe: "", repositories: ["acme/server"] }), []);
    assert.deepEqual(rules.check({ product: "Acme Server", vendor: "", cpe: "", repositories: ["server"] }), [LIST_ISSUES.repository]);
    assert.deepEqual(rules.check({ product: "", vendor: "", cpe: "jira", repositories: [] }), [LIST_ISSUES.emptyName, LIST_ISSUES.cpe]);
    assert.equal(rules.identity({ product: " Acme ", vendor: "Acme", cpe: "", repositories: [] }), "Acme\u0000Acme");
  });

  it("adds pasted rows in order, sets a field on the selected rows and removes them in bulk (slice 2)", () => {
    let rows = addRows(startRows([tech("Jira", ""), tech("Slack", "")]), [tech("A"), tech("B")]);
    assert.deepEqual(committedValues(rows).map((row) => row.product), ["Jira", "Slack", "A", "B"]);
    assert.deepEqual(changeCounts(rows), { added: 2, edited: 0, removed: 0 });
    const keyOf = (product: string) => rows.find((row) => row.value.product === product)!.key;
    const added = [keyOf("A"), keyOf("B")];
    rows = patchRows(rows, ["r0", "r1", added[0]], { vendor: "Acme" });
    assert.equal(rows.find((row) => row.key === "r0")?.value.vendor, "Acme");
    assert.equal(rows.find((row) => row.key === added[1])?.value.vendor, "");
    assert.deepEqual(changeCounts(rows), { added: 2, edited: 2, removed: 0 });
    rows = removeRows(rows, ["r1", added[0]]);
    assert.equal(rows.length, 3);
    assert.equal(rowState(rows.find((row) => row.key === "r1")!), "removed");
    // A removed row takes no bulk edit.
    rows = patchRows(rows, ["r1"], { vendor: "Other" });
    assert.equal(rows.find((row) => row.key === "r1")?.value.vendor, "Acme");
    assert.deepEqual(committedValues(rows).map((row) => row.product), ["Jira", "B"]);
  });

  it("leaves a row locked on a patched field as it is in a bulk edit (#1134)", () => {
    let rows = startRows([tech("Jira", ""), tech("Slack", "")]);
    const lockedOn = (value: { product: string }, field: string) => value.product === "Jira" && field === "vendor";
    rows = patchRows(rows, ["r0", "r1"], { vendor: "Acme" }, lockedOn);
    assert.equal(rows.find((row) => row.key === "r0")?.value.vendor, "");
    assert.equal(rows.find((row) => row.key === "r1")?.value.vendor, "Acme");
    // A field the row is not locked on still takes the edit.
    rows = patchRows(rows, ["r0"], { cpe: "cpe:/a:atlassian:jira" }, lockedOn);
    assert.equal(rows.find((row) => row.key === "r0")?.value.cpe, "cpe:/a:atlassian:jira");
  });

  it("suggests the values a field already holds, most used first, removed rows left out", () => {
    let rows = startRows([tech("Jira", "Atlassian"), tech("Confluence", "atlassian"), tech("Slack", "Salesforce"), tech("X", " "), tech("Old", "Gone")]);
    rows = removeRow(rows, "r4");
    assert.deepEqual(fieldSuggestions(rows, (value) => value.vendor), ["Atlassian", "Salesforce"]);
  });

  it("searches an open row on what is typed and on its value when opened, so it never drops out", () => {
    const rows = patchRow(startRows([tech("Jira", "Atlassian")]), "r0", { product: "Confluence" });
    const fields = rowSearchFields(rows[0], (value) => [value.product, value.vendor]);
    assert.ok(fields.includes("Jira"));
    assert.ok(fields.includes("Confluence"));
  });
});
