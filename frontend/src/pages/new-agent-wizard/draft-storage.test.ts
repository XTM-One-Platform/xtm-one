/**
 * Unit tests for the agent wizard draft kept across in-app navigation
 * - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  type DraftStorage,
  type ListableDraftStorage,
  type WizardDraft,
  MAX_WIZARD_DRAFT_CHARS,
  WIZARD_DRAFT_VERSION,
  clearAllWizardDrafts,
  WIZARD_DEFAULT_ICON,
  clearWizardDraft,
  hasWizardDraftContent,
  loadWizardDraft,
  parseWizardDraft,
  resolveDraftRestore,
  saveWizardDraft,
  serializeWizardDraft,
  withoutUnavailable,
  wizardDraftKey,
  wizardDraftOwner,
} from "./draft-storage.ts";

function memoryStorage(): ListableDraftStorage & { items: Map<string, string> } {
  const items = new Map<string, string>();
  return {
    items,
    get length() {
      return items.size;
    },
    key: (index) => [...items.keys()][index] ?? null,
    getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => void items.set(key, value),
    removeItem: (key) => void items.delete(key),
  };
}

const DRAFT: WizardDraft = {
  templateSlug: "email-triage",
  stepIndex: 3,
  reviewReached: false,
  name: "Mail Desk",
  icon: "Mail",
  description: "Triage my inbox",
  persona: "You triage email.",
  integrationIds: ["int-1"],
  toolIds: ["builtin:web_fetch"],
  kbIds: ["kb-1"],
  modelChoice: "provider-1::model-a",
  memory: { short: true, medium: false, persistent: true, user: true },
  guardrails: "Never send.",
  skillIds: ["skill-1"],
  handoverIds: [],
  budget: { cost: "5", tokens: "", calls: "", seconds: "" },
};

test("a saved draft is restored as it was, for the same user", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  assert.deepEqual(loadWizardDraft(storage, "user-a"), DRAFT);
});

test("a draft is private to the user who wrote it", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  assert.equal(loadWizardDraft(storage, "user-b"), null);
  assert.notEqual(wizardDraftKey("user-a"), wizardDraftKey("user-b"));
});

test("a draft started from scratch keeps its null template", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", { ...DRAFT, templateSlug: null });
  assert.equal(loadWizardDraft(storage, "user-a")?.templateSlug, null);
});

test("clearing forgets the draft", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  clearWizardDraft(storage, "user-a");
  assert.equal(loadWizardDraft(storage, "user-a"), null);
  assert.equal(storage.items.size, 0);
});

test("an unreadable, older or malformed draft is discarded, never half-applied", () => {
  assert.equal(parseWizardDraft(null), null);
  assert.equal(parseWizardDraft("not json"), null);
  assert.equal(parseWizardDraft(JSON.stringify({ version: WIZARD_DRAFT_VERSION + 1, draft: DRAFT })), null);
  assert.equal(parseWizardDraft(JSON.stringify({ version: WIZARD_DRAFT_VERSION })), null);
  const broken = (patch: Record<string, unknown>) =>
    JSON.stringify({ version: WIZARD_DRAFT_VERSION, draft: { ...DRAFT, ...patch } });
  assert.equal(parseWizardDraft(broken({ kbIds: [1, 2] })), null);
  assert.equal(parseWizardDraft(broken({ stepIndex: -1 })), null);
  assert.equal(parseWizardDraft(broken({ stepIndex: 1.5 })), null);
  assert.equal(parseWizardDraft(broken({ memory: { short: true } })), null);
  assert.equal(parseWizardDraft(broken({ budget: { cost: 5 } })), null);
  assert.equal(parseWizardDraft(broken({ templateSlug: 42 })), null);
  assert.deepEqual(parseWizardDraft(serializeWizardDraft(DRAFT)), DRAFT);
});

test("loading a malformed draft also removes it from storage", () => {
  const storage = memoryStorage();
  storage.setItem(wizardDraftKey("user-a"), "{broken");
  assert.equal(loadWizardDraft(storage, "user-a"), null);
  assert.equal(storage.items.size, 0);
});

test("no storage means no draft, and no error", () => {
  saveWizardDraft(null, "user-a", DRAFT);
  clearWizardDraft(null, "user-a");
  assert.equal(loadWizardDraft(null, "user-a"), null);
});

test("a storage that throws (quota, disabled) never breaks the wizard", () => {
  const throwing: DraftStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("quota");
    },
    removeItem: () => {
      throw new Error("denied");
    },
  };
  assert.doesNotThrow(() => saveWizardDraft(throwing, "user-a", DRAFT));
  assert.doesNotThrow(() => clearWizardDraft(throwing, "user-a"));
  assert.equal(loadWizardDraft(throwing, "user-a"), null);
  assert.doesNotThrow(() =>
    clearAllWizardDrafts({
      ...throwing,
      length: 1,
      key: () => {
        throw new Error("denied");
      },
    }),
  );
});

test("a failed write drops the older copy instead of leaving it to be restored as current", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  const full: DraftStorage = {
    getItem: storage.getItem,
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: storage.removeItem,
  };
  saveWizardDraft(full, "user-a", { ...DRAFT, persona: "A newer persona." });
  assert.equal(loadWizardDraft(storage, "user-a"), null);
});

test("a draft too large to keep is not stored, and the older copy goes with it", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  saveWizardDraft(storage, "user-a", { ...DRAFT, persona: "x".repeat(MAX_WIZARD_DRAFT_CHARS) });
  assert.equal(storage.items.size, 0);
  storage.setItem(
    wizardDraftKey("user-a"),
    JSON.stringify({ version: WIZARD_DRAFT_VERSION, draft: { ...DRAFT, persona: "x".repeat(MAX_WIZARD_DRAFT_CHARS) } }),
  );
  assert.equal(loadWizardDraft(storage, "user-a"), null);
  assert.equal(storage.items.size, 0);
});

// The names of knowledge bases created from the wizard now come from the
// flow node catalog, so the draft no longer keeps them. A draft written before
// that, by the first version, still carries `createdKbNames`.
test("a draft saved with created knowledge base names still restores, without them", () => {
  const storage = memoryStorage();
  const olderDraft = (createdKbNames: unknown) =>
    JSON.stringify({ version: WIZARD_DRAFT_VERSION, draft: { ...DRAFT, createdKbNames } });
  storage.setItem(wizardDraftKey("user-a"), olderDraft({ "kb-1": "Mail archive" }));
  const restored = loadWizardDraft(storage, "user-a");
  assert.deepEqual(restored, DRAFT);
  assert.equal(restored && "createdKbNames" in restored, false);
  // Saved again, it no longer carries the field.
  saveWizardDraft(storage, "user-a", restored!);
  assert.equal(storage.getItem(wizardDraftKey("user-a"))?.includes("createdKbNames"), false);
  // A map the first version would have refused is ignored the same way.
  assert.deepEqual(parseWizardDraft(olderDraft({ "kb-1": 7 })), DRAFT);
  assert.deepEqual(parseWizardDraft(olderDraft(["kb-1"])), DRAFT);
});

test("an explicit sign-out forgets every wizard draft of the tab, and nothing else", () => {
  const storage = memoryStorage();
  saveWizardDraft(storage, "user-a", DRAFT);
  saveWizardDraft(storage, wizardDraftOwner("user-b", "/xtm-one"), DRAFT);
  storage.setItem("chat:draft:conversation-1", "unrelated");
  clearAllWizardDrafts(storage);
  assert.deepEqual([...storage.items.keys()], ["chat:draft:conversation-1"]);
  assert.doesNotThrow(() => clearAllWizardDrafts(null));
});

test("a draft belongs to one instance: a base path scopes the key, the root keeps the plain one", () => {
  assert.equal(wizardDraftOwner("user-a", ""), "user-a");
  assert.notEqual(wizardDraftKey(wizardDraftOwner("user-a", "/one")), wizardDraftKey(wizardDraftOwner("user-a", "/two")));
  assert.notEqual(wizardDraftKey(wizardDraftOwner("user-a", "/one")), wizardDraftKey("user-a"));
});

test("a draft is rebuilt on its own template, waits for the template list, and is dropped when the template is gone", () => {
  const templates = [{ slug: "email-triage" }, { slug: "news-digest" }];
  assert.deepEqual(resolveDraftRestore(null, templates), { kind: "none" });
  assert.deepEqual(resolveDraftRestore(DRAFT, templates), { kind: "restore", template: templates[0] });
  assert.deepEqual(resolveDraftRestore({ ...DRAFT, templateSlug: null }, undefined), { kind: "restore", template: null });
  // The list failed to load: the draft is kept for when it does.
  assert.deepEqual(resolveDraftRestore(DRAFT, undefined), { kind: "wait" });
  assert.deepEqual(resolveDraftRestore({ ...DRAFT, templateSlug: "retired" }, templates), { kind: "drop" });
});

test("a draft is worth keeping once anything moved off its from-scratch defaults", () => {
  const blank: WizardDraft = {
    ...DRAFT,
    templateSlug: null,
    name: "",
    icon: WIZARD_DEFAULT_ICON,
    description: "",
    persona: "  ",
    integrationIds: [],
    toolIds: [],
    kbIds: [],
    modelChoice: "",
    memory: { short: true, medium: true, persistent: true, user: true },
    guardrails: "",
    skillIds: [],
    handoverIds: [],
    budget: { cost: "", tokens: "", calls: "", seconds: "" },
  };
  assert.equal(hasWizardDraftContent(blank), false);
  assert.equal(hasWizardDraftContent({ ...blank, templateSlug: "email-triage" }), true);
  assert.equal(hasWizardDraftContent({ ...blank, name: "Mail Desk" }), true);
  // An icon or an advanced option alone is a change too.
  assert.equal(hasWizardDraftContent({ ...blank, icon: "Mail" }), true);
  assert.equal(hasWizardDraftContent({ ...blank, modelChoice: "provider-1::model-a" }), true);
  assert.equal(hasWizardDraftContent({ ...blank, memory: { ...blank.memory, persistent: false } }), true);
  assert.equal(hasWizardDraftContent({ ...blank, budget: { ...blank.budget, calls: "20" } }), true);
  assert.equal(hasWizardDraftContent({ ...blank, skillIds: ["skill-1"] }), true);
});

test("restored selections the catalog no longer offers are dropped, later picks are kept", () => {
  const offered = [{ value: "kb-1" }, { value: "kb-new" }];
  assert.deepEqual(
    withoutUnavailable(["kb-1", "kb-deleted", "kb-new", "kb-picked-since"], ["kb-1", "kb-deleted"], offered),
    ["kb-1", "kb-new", "kb-picked-since"],
  );
  const unchanged = ["kb-1"];
  assert.equal(withoutUnavailable(unchanged, ["kb-1"], offered), unchanged);
});
