/**
 * Unit tests for the relay install commands - `yarn test:unit`.
 *
 * These are pasted on a customer's server as root: a value that breaks out of
 * its quotes there is a command injection, and a destination list the relay
 * cannot read leaves it reaching nothing.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  RELAY_TARGETS,
  binaryCommand,
  destinationList,
  installCommand,
  psQuote,
  shQuote,
  targetNeedsReusableKey,
  type InstallCommandInput,
} from "./install-commands.ts";

const INPUT: InstallCommandInput = {
  serverUrl: "https://xtm-one.example.com/",
  enrollmentKey: "rlenr-test-key",
  destinations: ["splunk.corp.local:8089", " *.tanium.corp:443 ", ""],
  scripts: {
    sh: "https://xtm-one.example.com/api/v1/relays/dist/xtm-one-relay-installer.sh",
    ps1: "https://xtm-one.example.com/api/v1/relays/dist/xtm-one-relay-installer.ps1",
  },
  version: "1.261002.1",
};

test("a value never leaves its quotes", () => {
  assert.equal(shQuote("a'b; rm -rf /"), "'a'\\''b; rm -rf /'");
  assert.equal(psQuote("a'b; Remove-Item"), "'a''b; Remove-Item'");
});

test("the allow-list is one comma-separated value, blanks dropped", () => {
  assert.equal(destinationList(INPUT.destinations), "splunk.corp.local:8089,*.tanium.corp:443");
});

test("the Linux and macOS command runs the served installer as root with the key and the list", () => {
  const cmd = installCommand("linux", INPUT)!;
  assert.match(cmd, /^curl -fsSL '.+xtm-one-relay-installer\.sh' \| sudo bash -s -- /);
  assert.match(cmd, /--server 'https:\/\/xtm-one\.example\.com'/);
  assert.match(cmd, /--key 'rlenr-test-key'/);
  assert.match(cmd, /--destinations 'splunk\.corp\.local:8089,\*\.tanium\.corp:443'/);
  assert.equal(installCommand("macos", INPUT), cmd);
});

test("the Windows command downloads the installer then runs it with quoted arguments", () => {
  const cmd = installCommand("windows", { ...INPUT, name: "Paris DC 1" })!;
  assert.match(cmd, /^iwr '.+xtm-one-relay-installer\.ps1' -OutFile xtm-one-relay-installer\.ps1; /);
  assert.match(cmd, /-Destinations 'splunk\.corp\.local:8089,\*\.tanium\.corp:443'/);
  assert.match(cmd, /-Name 'Paris DC 1'/);
});

test("a deployment that does not serve an installer offers no command for it", () => {
  const none = { ...INPUT, scripts: { sh: null, ps1: null } };
  assert.equal(installCommand("linux", none), null);
  assert.equal(installCommand("windows", none), null);
  assert.ok(installCommand("docker", none));
  assert.ok(installCommand("kubernetes", none));
});

test("the container commands keep the state on a volume and pin the served version", () => {
  const docker = installCommand("docker", INPUT)!;
  assert.match(docker, /-v xtm-one-relay:\/var\/lib\/xtm-one-relay/);
  assert.match(docker, /XTM_RELAY_ENROLLMENT_KEY='rlenr-test-key'/);
  assert.match(docker, /xtmone\/relay:1\.261002\.1$/);
  assert.match(installCommand("docker", { ...INPUT, version: null })!, /:latest$/);

  const k8s = installCommand("kubernetes", { ...INPUT, replicas: 3 })!;
  assert.match(k8s, /kind: StatefulSet/);
  assert.match(k8s, /replicas: 3/);
  // The key is a Secret, never a literal in the manifest.
  assert.equal(k8s.split("rlenr-test-key").length, 2);
  assert.match(k8s, /--from-literal=enrollment-key='rlenr-test-key'/);
  assert.match(k8s, /image: xtmone\/relay:1\.261002\.1\n\s+imagePullPolicy: IfNotPresent/);
  assert.doesNotMatch(installCommand("kubernetes", { ...INPUT, version: null })!, /imagePullPolicy/);
  assert.match(k8s, /volumeClaimTemplates/);
});

test("only a multi-replica target needs a reusable key", () => {
  for (const target of RELAY_TARGETS) assert.equal(targetNeedsReusableKey(target), false);
  assert.equal(targetNeedsReusableKey("kubernetes", 2), true);
});

test("without an installer, the binary's own enroll and run commands carry the key, once, in its quotes", () => {
  const none = { ...INPUT, scripts: { sh: null, ps1: null }, name: "mac local" };
  const unix = binaryCommand("macos", { ...none, enrollmentKey: "rlenr-a'b" });
  assert.deepEqual(unix.split("\n"), [
    "export XTM_RELAY_ALLOWED_DESTINATIONS='splunk.corp.local:8089,*.tanium.corp:443'",
    "xtm-one-relay enroll --server-url 'https://xtm-one.example.com' --key 'rlenr-a'\\''b' --name 'mac local'",
    "xtm-one-relay run",
  ]);
  const windows = binaryCommand("windows", none).split("\n");
  assert.equal(windows[0], "$env:XTM_RELAY_ALLOWED_DESTINATIONS = 'splunk.corp.local:8089,*.tanium.corp:443'");
  assert.match(windows[1], /^\.\\xtm-one-relay\.exe enroll --server-url 'https:\/\/xtm-one\.example\.com' --key 'rlenr-test-key' --name 'mac local'$/);
  assert.equal(windows[2], ".\\xtm-one-relay.exe run");
});
