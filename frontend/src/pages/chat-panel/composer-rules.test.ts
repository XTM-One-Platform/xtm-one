/**
 * The /chat composer's placeholder, Deep Investigation's sends and the
 * workspace a move files into - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { composerPlaceholder, deepInvestigationTakes, filingTarget, placeholderAgentName } from "./composer-rules.ts";

const t = (key: string, values: Record<string, string> = {}) =>
  key.replace(/\{(\w+)\}/g, (_, name: string) => values[name] ?? `{${name}}`);

test("the placeholder names the agent a message goes to", () => {
  assert.equal(composerPlaceholder("Analyst", { deepInvestigation: false, platformName: "XTM One" }, t), "Message Analyst…");
});

test("an open conversation's placeholder names its own agent, not the one the menu picked", () => {
  const agents = [
    { id: "a-1", name: "CTEM Assistant" },
    { id: "a-2", name: "CTI STIX Consumer" },
  ];
  // Continued with another agent: the menu still has the first one.
  assert.equal(placeholderAgentName("a-2", agents, "CTEM Assistant"), "CTI STIX Consumer");
  assert.equal(placeholderAgentName(null, agents, "CTEM Assistant"), null);
  // No conversation open: the agent picked.
  assert.equal(placeholderAgentName(undefined, agents, "CTEM Assistant"), "CTEM Assistant");
  assert.equal(placeholderAgentName("gone", agents, "CTEM Assistant"), "CTEM Assistant");
});

test("the General Assistant's placeholder names the platform and says @ mentions", () => {
  assert.equal(
    composerPlaceholder(null, { deepInvestigation: false, platformName: "XTM One" }, t),
    "Message XTM One… (type @ to mention an agent or a conversation)",
  );
  assert.equal(
    composerPlaceholder(null, { deepInvestigation: false, platformName: "" }, t),
    "Message Assistant… (type @ to mention an agent or a conversation)",
  );
});

test("Deep Investigation's placeholder asks for a subject", () => {
  assert.equal(
    composerPlaceholder("Analyst", { deepInvestigation: true, platformName: "XTM One" }, t),
    "Enter a subject to investigate…",
  );
});

test("Deep Investigation refuses a composer message carrying files, and takes one without as its subject", () => {
  const draft = (fileCount: number) => ({ source: "composer" as const, turnRunning: false, fileCount });
  assert.equal(deepInvestigationTakes(draft(2), true), "files");
  assert.equal(deepInvestigationTakes(draft(0), true), "subject");
  assert.equal(deepInvestigationTakes({ source: "composer", turnRunning: false }, true), "subject");
});

test("Deep Investigation leaves alone a send while it is off, a host's send and a steer", () => {
  assert.equal(deepInvestigationTakes({ source: "composer", turnRunning: false, fileCount: 2 }, false), null);
  assert.equal(deepInvestigationTakes({ source: "host", turnRunning: false, fileCount: 0 }, true), null);
  assert.equal(deepInvestigationTakes({ source: "composer", turnRunning: true, fileCount: 2 }, true), null);
});

test("a move files into the platform's workspace of that id; taking one out files into none", () => {
  const workspaces = [{ id: "w-1", name: "Team" }, { id: "w-2", name: "Mine" }];
  assert.deepEqual(filingTarget({ id: "w-1" }, workspaces), workspaces[0]);
  assert.equal(filingTarget(null, workspaces), undefined);
  assert.equal(filingTarget({ id: "w-9" }, workspaces), undefined);
  assert.equal(filingTarget({ id: "w-1" }, undefined), undefined);
});
