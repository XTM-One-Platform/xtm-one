import assert from "node:assert/strict";
import { test } from "node:test";

import { groupAllows, parseDestination, relayMayAllow, serviceUrlOf } from "./relay-destinations.ts";

const splunk = parseDestination("https://Splunk.corp.local:8089/services")!;
const tanium = parseDestination("https://10.20.4.5/api")!;

test("a URL reads as its host and port", () => {
  assert.deepEqual(splunk, { host: "splunk.corp.local", port: 8089 });
  assert.deepEqual(tanium, { host: "10.20.4.5", port: 443 });
  assert.deepEqual(parseDestination("http://intranet."), { host: "intranet", port: 80 });
  assert.equal(parseDestination("ftp://x"), null);
  assert.equal(parseDestination("not a url"), null);
  assert.equal(parseDestination(""), null);
});

test("an integration's service URL is read from its usual settings", () => {
  assert.equal(serviceUrlOf({ base_url: " https://splunk:8089 ", url: "https://other" }), "https://splunk:8089");
  assert.equal(serviceUrlOf({ instance_url: "https://tanium" }), "https://tanium");
  assert.equal(serviceUrlOf({ base_url: "", username: "x" }), null);
  assert.equal(serviceUrlOf({ base_url: 3 }), null);
  assert.equal(serviceUrlOf(null), null);
});

test("the group's list is matched as the platform matches it", () => {
  assert.equal(groupAllows(null, splunk), true);
  assert.equal(groupAllows([], splunk), false);
  assert.equal(groupAllows(["splunk.corp.local:8089"], splunk), true);
  assert.equal(groupAllows(["splunk.corp.local:443"], splunk), false);
  assert.equal(groupAllows(["*.corp.local"], splunk), true);
  assert.equal(groupAllows(["*.local:8089"], splunk), true);
  assert.equal(groupAllows(["corp.local"], splunk), false);
  assert.equal(groupAllows(["*"], splunk), true);
  // The platform compares the host as written: a name never matches a CIDR.
  assert.equal(groupAllows(["10.0.0.0/8"], splunk), false);
  assert.equal(groupAllows(["10.20.0.0/16:443"], tanium), true);
  assert.equal(groupAllows(["10.21.0.0/16"], tanium), false);
  assert.equal(groupAllows(["10.20.4.5"], tanium), true);
});

test("a relay's list refuses only what no resolution could admit", () => {
  assert.equal(relayMayAllow([], splunk), false);
  assert.equal(relayMayAllow(null, splunk), false);
  assert.equal(relayMayAllow(["splunk.corp.local:8089"], splunk), true);
  assert.equal(relayMayAllow(["tanium.corp.local"], splunk), false);
  // The relay checks a name on the addresses it resolves to.
  assert.equal(relayMayAllow(["10.0.0.0/8"], splunk), true);
  assert.equal(relayMayAllow(["10.0.0.0/8:443"], splunk), false);
  assert.equal(relayMayAllow(["10.20.0.0/16"], tanium), true);
  assert.equal(relayMayAllow(["192.168.0.0/16"], tanium), false);
  assert.equal(relayMayAllow(["*.corp.local"], tanium), false);
  assert.equal(relayMayAllow(["*"], tanium), true);
});
