/**
 * The paused-flow notice lets an administrator reach the controls that keep
 * the flow running - `yarn test:unit`.
 *
 * The notice names two remedies (transfer the flow to an active owner, make it
 * company-managed); both live in the flow builder's admin-only Owner and access
 * drawer, which opens on `?panel=owner-access`. The component cannot
 * render under `node --test`, so this reads it: the builder hands the notice
 * that URL through the drawer's own `ownerAccessUrl()`, the notice links to it
 * for an admin only, through the router, with a label every locale translates
 * and no native `title`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ownerAccessUrl } from "../../pages/flow-builder/panel-param.ts";

const read = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");

const notice = read("./owner-deactivated-notice.tsx");
const builder = read("../../pages/flow-builder/index.tsx");

test("the flow builder hands its paused notice the Owner and access URL", () => {
  const call = builder.slice(builder.indexOf("<OwnerDeactivatedNotice"));
  const props = call.slice(0, call.indexOf("/>"));
  assert.match(props, /entity="flow"/);
  assert.match(props, /ownerAccessHref=\{ownerAccessUrl\(flow\.id\)\}/);
  assert.equal(ownerAccessUrl("f1"), "/flows/library/f1?panel=owner-access");
});

test("the notice links to it through the router, for an admin only", () => {
  assert.match(notice, /ownerAccessHref\?: string;/);
  const admin = notice.slice(notice.indexOf("{isAdmin && (ownerId || ownerAccessHref) && ("));
  assert.ok(admin.length < notice.length, "the admin-only block is missing");
  const link = admin.slice(admin.indexOf("{ownerAccessHref && ("), admin.indexOf("{ownerId && ("));
  assert.match(link, /<Link to=\{ownerAccessHref\}>/);
  assert.match(link, /t_i18n\("Owner and access"\)/);
  assert.doesNotMatch(notice, /\btitle=/);
});

test("every locale translates the link's label", () => {
  for (const lang of ["en", "fr", "es", "ja"]) {
    const catalog = JSON.parse(read(`../../i18n/lang/${lang}.json`)) as Record<string, string>;
    assert.ok(catalog["Owner and access"], `${lang} lacks "Owner and access"`);
  }
});
