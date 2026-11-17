/**
 * Unit tests for the share dialogs' draft - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  canonicalSharing,
  sharingAudience,
  sharingChanged,
  sharingPayloadGroups,
  type SharingDraft,
} from "./sharing-draft.ts";

const PRIVATE: SharingDraft = {
  companyManaged: false,
  accessRestricted: false,
  groupIds: null,
  groupPermissions: null,
  userIds: null,
  userPermissions: null,
};

// Echo the key and its values, so a test reads which sentence was picked.
const t = (key: string, values?: Record<string, unknown>) =>
  values ? `${key} ${JSON.stringify(values)}` : key;

test("an empty list and no list are the same stored state", () => {
  assert.equal(
    canonicalSharing({ ...PRIVATE, groupIds: [], userIds: [] }),
    canonicalSharing(PRIVATE),
  );
  assert.equal(sharingChanged({ ...PRIVATE, groupIds: [] }, PRIVATE), false);
});

test("a missing permission reads as read-only, and order does not matter", () => {
  const a = { ...PRIVATE, userIds: ["u2", "u1"], userPermissions: { u2: "config" } };
  const b = { ...PRIVATE, userIds: ["u1", "u2"], userPermissions: { u1: "read_only", u2: "config" } };
  assert.equal(sharingChanged(a, b), false);
  assert.equal(sharingChanged(a, { ...b, userPermissions: { u1: "config", u2: "config" } }), true);
});

test("an unrestricted company-managed entity keeps no grants", () => {
  const published = { ...PRIVATE, companyManaged: true, groupIds: ["g1"] };
  assert.equal(sharingChanged(published, { ...PRIVATE, companyManaged: true }), false);
  assert.equal(
    sharingChanged({ ...published, accessRestricted: true }, { ...PRIVATE, companyManaged: true }),
    true,
  );
});

test("the audience says who the draft lets in", () => {
  assert.deepEqual(sharingAudience(PRIVATE, t), {
    kind: "private",
    text: "Only you, until you share it above.",
  });
  assert.equal(
    sharingAudience({ ...PRIVATE, groupIds: [] }, t).text,
    "Only you: no one is listed above yet.",
  );
  const people = sharingAudience({ ...PRIVATE, userIds: ["u1", "u2"] }, t);
  assert.equal(people.kind, "shared");
  assert.match(people.text, /"people":2/);
  const both = sharingAudience({ ...PRIVATE, userIds: ["u1"], groupIds: ["g1"] }, t);
  assert.match(both.text, /"people":1,"groups":1/);
  assert.equal(sharingAudience({ ...PRIVATE, companyManaged: true }, t).kind, "company");
  assert.equal(
    sharingAudience({ ...PRIVATE, companyManaged: true, accessRestricted: true }, t).kind,
    "restricted",
  );
});

test("an empty group list is sent as null, with its permissions", () => {
  assert.deepEqual(sharingPayloadGroups({ ...PRIVATE, groupIds: [], groupPermissions: {} }), {
    shared_group_ids: null,
    shared_group_permissions: null,
  });
  assert.deepEqual(
    sharingPayloadGroups({ ...PRIVATE, groupIds: ["g1"], groupPermissions: { g1: "config" } }),
    { shared_group_ids: ["g1"], shared_group_permissions: { g1: "config" } },
  );
});
