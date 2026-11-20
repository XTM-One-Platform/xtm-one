/**
 * Unit tests for the OAuth default form's stored secrets - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  EXTRA_CONFIG_MASK,
  domainList,
  extraConfigPatch,
  extraFieldDefaults,
  extraFieldProblem,
  hasInvalidExtraField,
  maskedStoredSecrets,
} from "./extra-fields.ts";

const LIMIT = { kind: "integer", minimum: 0, maximum: 1000, default: "50" };
const DOMAINS = { kind: "domains" };

test("a stored domain list reads one entry per line, space, comma or semicolon", () => {
  assert.deepEqual(domainList("a.com\nb.org, c.net;d.io"), ["a.com", "b.org", "c.net", "d.io"]);
  assert.deepEqual(domainList(undefined), []);
});

test("a domain list names the entries that are not domain names", () => {
  assert.equal(extraFieldProblem("example.com\n@test.org", DOMAINS), null);
  assert.deepEqual(extraFieldProblem("example.com\nnot a domain", DOMAINS), { badDomains: ["not", "a", "domain"] });
  assert.deepEqual(extraFieldProblem("bob@example.com", DOMAINS), { badDomains: ["bob@example.com"] });
});

test("an integer outside its bounds is out of range, blank is not judged", () => {
  assert.deepEqual(extraFieldProblem("5000", LIMIT), { outOfRange: true });
  assert.deepEqual(extraFieldProblem("-1", LIMIT), { outOfRange: true });
  assert.deepEqual(extraFieldProblem("2.5", LIMIT), { outOfRange: true });
  assert.equal(extraFieldProblem("1000", LIMIT), null);
  assert.equal(extraFieldProblem("", LIMIT), null);
});

test("the form refuses to save while a typed field breaks its rule, and prefills the defaults", () => {
  const specs = { daily_limit: LIMIT, allowed_domains: DOMAINS };
  assert.equal(hasInvalidExtraField({ daily_limit: "5000" }, specs), true);
  assert.equal(hasInvalidExtraField({ daily_limit: "50", allowed_domains: "example.com" }, specs), false);
  assert.deepEqual(extraFieldDefaults(specs), { daily_limit: "50" });
});

test("a stored secret starts as the mask, so saving the form untouched keeps it", () => {
  assert.deepEqual(maskedStoredSecrets(["webhook_secret"]), { webhook_secret: EXTRA_CONFIG_MASK });
});

test("nothing stored starts empty", () => {
  assert.deepEqual(maskedStoredSecrets([]), {});
  assert.deepEqual(maskedStoredSecrets(undefined), {});
});

test("the edit form's patch removes, as null, every stored key the form dropped", () => {
  assert.deepEqual(
    extraConfigPatch({ base_url: "https://b" }, { base_url: "https://a", credential_mode: "x" }),
    { base_url: "https://b", credential_mode: null },
  );
});

test("the edit form's patch keeps what it holds and adds nothing when nothing is stored", () => {
  assert.deepEqual(extraConfigPatch({ backend: "gw" }, null), { backend: "gw" });
  assert.deepEqual(extraConfigPatch({}, undefined), {});
});
