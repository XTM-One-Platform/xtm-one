/**
 * Unit tests for the OAuth default form's stored secrets - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  EXTRA_CONFIG_MASK,
  domainList,
  extraConfigPatch,
  EXTRA_FIELD_NAME_MAX,
  extraFieldDefaults,
  extraFieldKeyName,
  extraFieldLabels,
  extraFieldProblem,
  fieldNameInSentence,
  hasInvalidExtraField,
  isFieldName,
  isSecretExtraKey,
  maskedStoredSecrets,
  splitExtraFieldLabel,
} from "./extra-fields.ts";

test("a catalog label splits into its name, its explanation and whether it is optional", () => {
  assert.deepEqual(
    splitExtraFieldLabel("Webhook secret (optional \u2014 for signature verification of inbound events)"),
    { name: "Webhook secret", detail: "For signature verification of inbound events", optional: true },
  );
  assert.deepEqual(
    splitExtraFieldLabel("Webhook HMAC signing secret (X-Aikido-Webhook-Signature)"),
    { name: "Webhook HMAC signing secret", detail: "Carried in the X-Aikido-Webhook-Signature header", optional: false },
  );
  assert.deepEqual(
    splitExtraFieldLabel("Linked-device gateway: a long random secret you choose"),
    { name: "Linked-device gateway", detail: "A long random secret you choose", optional: false },
  );
  assert.deepEqual(splitExtraFieldLabel("Base URL"), { name: "Base URL", detail: null, optional: false });
  assert.deepEqual(splitExtraFieldLabel("Region (optional)"), { name: "Region", detail: null, optional: true });
  assert.deepEqual(splitExtraFieldLabel("Azure AD Tenant ID (required)"), { name: "Azure AD Tenant ID", detail: null, optional: false });
});

test("a connection string is masked like a key, the Event Hub coordinates are not", () => {
  assert.equal(isSecretExtraKey("event_hub_connection_string"), true);
  assert.equal(isSecretExtraKey("event_hub_namespace"), false);
  assert.equal(isSecretExtraKey("event_hub_name"), false);
  assert.equal(isSecretExtraKey("event_hub_consumer_group"), false);
});

test("the Defender Event Hub labels split into one name each", () => {
  const labels = extraFieldLabels({
    event_hub_namespace: "Event Hub namespace: the <name>.servicebus.windows.net host the Defender XDR Streaming API sends alert events to",
    event_hub_name: "Event Hub name: the hub of that namespace the Streaming API writes to",
    event_hub_consumer_group: "Event Hub consumer group: optional, $Default when empty",
    event_hub_connection_string: "Event Hub connection string: optional, used instead of the Entra app's Azure Event Hubs Data Receiver role",
  });
  assert.equal(labels.event_hub_namespace.name, "Event Hub namespace");
  assert.equal(labels.event_hub_name.name, "Event Hub name");
  assert.equal(labels.event_hub_consumer_group.name, "Event Hub consumer group");
  assert.equal(labels.event_hub_consumer_group.optional, true);
  assert.equal(labels.event_hub_connection_string.name, "Event Hub connection string");
  assert.equal(labels.event_hub_connection_string.optional, true);
});

test("a label that does not open with a name is named off its key and kept whole in the bubble", () => {
  // A number, then an unbalanced split: the name comes from the key.
  assert.deepEqual(
    splitExtraFieldLabel("5432 (session pooler or direct, default) or 6543 (transaction pooler)", "db_port"),
    { name: "DB port", detail: "5432 (session pooler or direct, default) or 6543 (transaction pooler)", optional: false },
  );
  // A sentence (an article first, or speaking to the reader) is not a name.
  assert.deepEqual(
    splitExtraFieldLabel("Your Entra tenant (directory) ID (required)", "tenant_id"),
    { name: "Tenant ID", detail: "Your Entra tenant (directory) ID", optional: false },
  );
  assert.deepEqual(
    splitExtraFieldLabel("The database host", "db_host"),
    { name: "DB host", detail: "The database host", optional: false },
  );
  assert.equal(splitExtraFieldLabel("Client ID the partner issued you", "client_id").name, "Client ID");
  // A spaced dash separates like a colon; a trailing dashed REQUIRED is a marker.
  assert.deepEqual(
    splitExtraFieldLabel("API Key ID (x-xdr-auth-id) \u2014 REQUIRED", "api_key_id"),
    { name: "API Key ID", detail: "Carried in the x-xdr-auth-id header", optional: false },
  );
  // An explanation that opens with a URL, a header, a key or a quote is shown as written.
  assert.deepEqual(
    splitExtraFieldLabel("Cortex XDR tenant API host \u2014 https://api-<fqdn> (from your API key's CURL example URL)", "base_url"),
    { name: "Cortex XDR tenant API host", detail: "https://api-<fqdn> (from your API key's CURL example URL)", optional: false },
  );
  assert.equal(splitExtraFieldLabel("Header: x-api-key, sent on every call", "header").detail, "x-api-key, sent on every call");
  assert.equal(splitExtraFieldLabel("Key type: 'standard' or 'advanced'", "key_type").detail, "'standard' or 'advanced'");
  assert.equal(splitExtraFieldLabel("Region: where the tenant is hosted", "region").detail, "Where the tenant is hosted");
  assert.equal(splitExtraFieldLabel("Mode: linked-device gateway or cloud", "mode").detail, "Linked-device gateway or cloud");
  // A bare code token in a trailing parenthesis that is not a header is not an explanation.
  assert.deepEqual(splitExtraFieldLabel("Project ref (project_ref)", "project_ref"), { name: "Project ref", detail: null, optional: false });
  assert.deepEqual(
    splitExtraFieldLabel("Cortex XSOAR base URL \u2014 XSOAR 8/Cloud", "base_url"),
    { name: "Cortex XSOAR base URL", detail: "XSOAR 8/Cloud", optional: false },
  );
  // A leading "Optional" marks the field and leaves a sentence: named off its key.
  assert.deepEqual(
    splitExtraFieldLabel("Optional \u2014 restrict the integration to one project: its reference", "project_ref"),
    { name: "Project ref", detail: "Restrict the integration to one project: its reference", optional: true },
  );
  // A sentence before the parenthesis is not a name.
  assert.deepEqual(
    splitExtraFieldLabel("Elasticsearch URL for hunting (ES|QL/EQL/_search), e.g. https://my.es.io (optional)", "es_url"),
    { name: "ES URL", detail: "Elasticsearch URL for hunting (ES|QL/EQL/_search), e.g. https://my.es.io", optional: true },
  );
});

test("a group shared by several fields is not their name", () => {
  const labels = extraFieldLabels({
    phone_number_id: "Meta Cloud API: phone number ID of the business number",
    waba_id: "Meta Cloud API: WhatsApp Business Account ID (optional, for template management)",
    base_url: "Linked-device gateway: API base URL",
  });
  assert.deepEqual(labels.phone_number_id, { name: "Phone number ID", detail: "Meta Cloud API: phone number ID of the business number", optional: false });
  assert.equal(labels.waba_id.name, "WABA ID");
  assert.equal(labels.waba_id.optional, true);
  assert.deepEqual(labels.base_url, { name: "Linked-device gateway", detail: "API base URL", optional: false });
});

test("a key reads as a name, acronyms kept", () => {
  assert.equal(extraFieldKeyName("client_id"), "Client ID");
  assert.equal(extraFieldKeyName("db_ca_cert"), "DB CA cert");
});

test("a field name reads inside a sentence, acronyms and proper nouns kept", () => {
  assert.equal(fieldNameInSentence("Webhook secret"), "webhook secret");
  assert.equal(fieldNameInSentence("API key"), "API key");
  assert.equal(fieldNameInSentence("URL"), "URL");
  assert.equal(fieldNameInSentence("Meta Cloud API"), "Meta Cloud API");
  // One rule for every field of a form.
  assert.equal(fieldNameInSentence("Base URL"), "base URL");
  assert.equal(fieldNameInSentence("Phone number ID"), "phone number ID");
  assert.equal(fieldNameInSentence("Kibana space ID"), "Kibana space ID");
  assert.equal(fieldNameInSentence("Databricks workspace URL"), "Databricks workspace URL");
});

test("a name holds no dash, parenthesis, shouted word or sentence", () => {
  assert.equal(isFieldName("API Key ID"), true);
  assert.equal(isFieldName("API Key ID \u2014 REQUIRED"), false);
  assert.equal(isFieldName("Tenant (directory) ID"), false);
  assert.equal(isFieldName("The database"), false);
  assert.equal(isFieldName("Client ID the partner issued you"), false);
});

/** Every one-line `"key": "label"` entry of each `extra_fields` block of the provider catalog. */
function catalogExtraFields(): Record<string, string>[] {
  const source = readFileSync(new URL("../../../backend/app/api/v1/integration_catalog.py", import.meta.url), "utf8");
  const blocks: Record<string, string>[] = [];
  const lines = source.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    if (!/"extra_fields": \{\s*$/.test(lines[i])) continue;
    const block: Record<string, string> = {};
    for (i += 1; i < lines.length && !/^\s*\},?\s*$/.test(lines[i]); i += 1) {
      const entry = /^\s*"([a-z0-9_]+)": "((?:[^"\\]|\\.)*)",?\s*$/.exec(lines[i]);
      if (entry) block[entry[1]] = JSON.parse(`"${entry[2]}"`) as string;
    }
    if (Object.keys(block).length) blocks.push(block);
  }
  return blocks;
}

test("every provider field of the catalog gets its own short name", () => {
  const blocks = catalogExtraFields();
  assert.ok(blocks.length > 50, `only ${blocks.length} extra_fields blocks read from the catalog`);
  const problems: string[] = [];
  for (const fields of blocks) {
    const labels = extraFieldLabels(fields);
    const seen = new Set<string>();
    for (const [key, { name }] of Object.entries(labels)) {
      if (!isFieldName(name) || name.length > EXTRA_FIELD_NAME_MAX || seen.has(name.toLowerCase())) problems.push(`${key}: ${name}`);
      seen.add(name.toLowerCase());
    }
  }
  assert.deepEqual(problems, []);
});

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
