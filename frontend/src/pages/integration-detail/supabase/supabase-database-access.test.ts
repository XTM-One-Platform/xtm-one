/**
 * Unit tests for the Supabase Database access tab's logic - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  allowedFunctions,
  connectionValue,
  draftFromExtra,
  extraFromDraft,
  needsPassword,
  parseConnection,
  probeBody,
  withoutDatabaseRole,
} from "./supabase-database-access.ts";

const MASK = "********";
const STORED = {
  project_ref: "abcdefghijklmnopqrst",
  db_role: "agent_reader",
  db_password: MASK,
  db_host: "aws-0-eu-west-1.pooler.supabase.com",
  db_functions: "public.get_document, public.get_document_excerpt",
  other: "kept",
};

test("a stored config gives its draft, the password never", () => {
  assert.deepEqual(draftFromExtra(STORED), {
    projectRef: "abcdefghijklmnopqrst",
    host: "aws-0-eu-west-1.pooler.supabase.com",
    port: "5432",
    database: "postgres",
    role: "agent_reader",
    password: "",
    caCert: "",
  });
  assert.deepEqual(allowedFunctions(STORED), ["public.get_document", "public.get_document_excerpt"]);
  assert.deepEqual(allowedFunctions({ db_functions: ["a.b", "a.b", " c.d "] }), ["a.b", "c.d"]);
  assert.deepEqual(allowedFunctions(null), []);
});

test("the probe sends a typed password only", () => {
  const draft = draftFromExtra(STORED);
  assert.equal("db_password" in probeBody(draft), false);
  assert.equal(probeBody({ ...draft, password: "typed" }).db_password, "typed");
});

test("a save keeps the other keys and sends an untouched password back as the mask", () => {
  const draft = { ...draftFromExtra(STORED), host: " AWS-0-EU-WEST-1.pooler.supabase.com " };
  assert.deepEqual(extraFromDraft(STORED, draft, ["public.get_document", "public.get_document"]), {
    project_ref: "abcdefghijklmnopqrst",
    db_role: "agent_reader",
    db_password: MASK,
    db_host: "aws-0-eu-west-1.pooler.supabase.com",
    db_port: "5432",
    db_name: "postgres",
    db_functions: "public.get_document",
    other: "kept",
  });
  const typed = extraFromDraft(STORED, { ...draft, password: "new", caCert: "-----BEGIN CERTIFICATE-----" }, []);
  assert.equal(typed.db_password, "new");
  assert.equal(typed.db_ca_cert, "-----BEGIN CERTIFICATE-----");
  assert.equal(typed.db_functions, "");
  // Nothing stored and nothing typed: no password key at all.
  assert.equal("db_password" in extraFromDraft({}, draft, []), false);
  // The update keeps a key left out: an emptied stored certificate is sent as null.
  assert.equal(extraFromDraft({ ...STORED, db_ca_cert: "-----BEGIN CERTIFICATE-----" }, draft, []).db_ca_cert, null);
  assert.equal("db_ca_cert" in extraFromDraft(STORED, draft, []), false);
});

test("removing the role sends every stored db key as null and keeps the rest", () => {
  // The update merges key by key and keeps a key left out: only null removes it.
  assert.deepEqual(withoutDatabaseRole({ ...STORED, db_ca_cert: "x", db_port: "6543", db_name: "postgres" }), {
    project_ref: "abcdefghijklmnopqrst",
    other: "kept",
    db_role: null,
    db_password: null,
    db_host: null,
    db_functions: null,
    db_ca_cert: null,
    db_port: null,
    db_name: null,
  });
  assert.deepEqual(withoutDatabaseRole({ project_ref: "abcdefghijklmnopqrst" }), { project_ref: "abcdefghijklmnopqrst" });
});

test("a connection choice round-trips", () => {
  assert.equal(connectionValue("Host.pooler.supabase.com", 6543), "host.pooler.supabase.com:6543");
  assert.deepEqual(parseConnection("host.pooler.supabase.com:6543"), { host: "host.pooler.supabase.com", port: "6543" });
  assert.deepEqual(parseConnection("db.x.supabase.co"), { host: "db.x.supabase.co", port: "5432" });
});

test("the stored password is kept only for the role, host and project it was saved for", () => {
  const draft = draftFromExtra(STORED);
  assert.equal(needsPassword(STORED, draft), false);
  assert.equal(needsPassword(STORED, { ...draft, host: draft.host.toUpperCase() }), false);
  assert.equal(needsPassword(STORED, { ...draft, role: "reporting" }), true);
  assert.equal(needsPassword(STORED, { ...draft, host: "aws-0-us-east-1.pooler.supabase.com" }), true);
  assert.equal(needsPassword(STORED, { ...draft, projectRef: "zyxwvutsrqponmlkjihg" }), true);
  assert.equal(needsPassword(STORED, { ...draft, role: "reporting", password: "typed" }), false);
  assert.equal(needsPassword({}, draft), true);
  // The stored password, saved for another role, is removed rather than kept.
  assert.equal(extraFromDraft(STORED, { ...draft, role: "reporting" }, []).db_password, null);
});
