/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The commands that install and enroll a relay, one per target.
 *
 * Pure on purpose (no React, no i18n) so the exact text a person pastes on a
 * server is unit-tested. Every value is single-quoted for the shell it runs
 * in: a destination pattern carries `*` (`*.corp.local:443`) and a relay name
 * may carry spaces. The enrollment key and the platform URL never need more,
 * but are quoted the same way so the commands read alike.
 */

export type RelayTarget = "linux" | "macos" | "windows" | "docker" | "kubernetes";

export const RELAY_TARGETS: readonly RelayTarget[] = ["linux", "macos", "windows", "docker", "kubernetes"];

/** The published image the Docker and Kubernetes commands run. */
const RELAY_IMAGE = "xtmone/relay";

/** Where the image keeps its configuration, credential and state. */
const RELAY_CONTAINER_STATE_DIR = "/var/lib/xtm-one-relay";

export interface InstallCommandInput {
  /** The platform the relay connects to (`https://...`, no trailing slash needed). */
  serverUrl: string;
  /** The raw `rlenr-` enrollment key. */
  enrollmentKey: string;
  /** The relay's local allow-list - the hard boundary of what it reaches. */
  destinations: readonly string[];
  /** How the relay shows in XTM One; the hostname when empty. */
  name?: string;
  /** Installer URLs this deployment serves (`/relays/meta`). */
  scripts: { sh: string | null; ps1: string | null };
  /** The relay build this deployment serves; `latest` when unknown. */
  version?: string | null;
  /** Replicas of the Kubernetes StatefulSet. */
  replicas?: number;
}

/** `value` as one single-quoted POSIX shell word. */
export function shQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** `value` as one single-quoted PowerShell string. */
export function psQuote(value: string): string {
  return `'${value.replace(/'/g, "''")}'`;
}

/** The allow-list as the relay reads it from one variable: comma-separated. */
export function destinationList(destinations: readonly string[]): string {
  return destinations.map((d) => d.trim()).filter(Boolean).join(",");
}

function trimSlash(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

function unixCommand(input: InstallCommandInput): string | null {
  if (!input.scripts.sh) return null;
  const parts = [
    `curl -fsSL ${shQuote(input.scripts.sh)} | sudo bash -s --`,
    `--server ${shQuote(trimSlash(input.serverUrl))}`,
    `--key ${shQuote(input.enrollmentKey)}`,
    `--destinations ${shQuote(destinationList(input.destinations))}`,
  ];
  if (input.name?.trim()) parts.push(`--name ${shQuote(input.name.trim())}`);
  return parts.join(" ");
}

function windowsCommand(input: InstallCommandInput): string | null {
  if (!input.scripts.ps1) return null;
  const args = [
    `-Server ${psQuote(trimSlash(input.serverUrl))}`,
    `-Key ${psQuote(input.enrollmentKey)}`,
    `-Destinations ${psQuote(destinationList(input.destinations))}`,
  ];
  if (input.name?.trim()) args.push(`-Name ${psQuote(input.name.trim())}`);
  return [
    `iwr ${psQuote(input.scripts.ps1)} -OutFile xtm-one-relay-installer.ps1;`,
    `powershell -ExecutionPolicy Bypass -File .\\xtm-one-relay-installer.ps1 ${args.join(" ")}`,
  ].join(" ");
}

function imageRef(version?: string | null): string {
  return `${RELAY_IMAGE}:${version?.trim() || "latest"}`;
}

function dockerCommand(input: InstallCommandInput): string {
  const lines = [
    "docker run -d --name xtm-one-relay --restart unless-stopped",
    `  -v xtm-one-relay:${RELAY_CONTAINER_STATE_DIR}`,
    `  -e XTM_RELAY_SERVER_URL=${shQuote(trimSlash(input.serverUrl))}`,
    `  -e XTM_RELAY_ENROLLMENT_KEY=${shQuote(input.enrollmentKey)}`,
    `  -e XTM_RELAY_ALLOWED_DESTINATIONS=${shQuote(destinationList(input.destinations))}`,
  ];
  if (input.name?.trim()) lines.push(`  -e XTM_RELAY_NAME=${shQuote(input.name.trim())}`);
  lines.push(`  ${imageRef(input.version)}`);
  return lines.join(" \\\n");
}

/** A YAML double-quoted scalar. */
function yamlString(value: string): string {
  return JSON.stringify(value);
}

function kubernetesCommand(input: InstallCommandInput): string {
  const replicas = Math.max(1, Math.min(10, Math.floor(input.replicas ?? 2)));
  const env = [
    `            - name: XTM_RELAY_SERVER_URL\n              value: ${yamlString(trimSlash(input.serverUrl))}`,
    "            - name: XTM_RELAY_ENROLLMENT_KEY\n              valueFrom:\n                secretKeyRef:\n                  name: xtm-one-relay\n                  key: enrollment-key",
    `            - name: XTM_RELAY_ALLOWED_DESTINATIONS\n              value: ${yamlString(destinationList(input.destinations))}`,
    "            - name: XTM_RELAY_HEALTH_LISTEN\n              value: \"0.0.0.0:8080\"",
  ];
  return [
    "kubectl create namespace xtm-one-relay",
    `kubectl -n xtm-one-relay create secret generic xtm-one-relay --from-literal=enrollment-key=${shQuote(input.enrollmentKey)}`,
    "kubectl -n xtm-one-relay apply -f - <<'EOF'",
    "apiVersion: apps/v1",
    "kind: StatefulSet",
    "metadata:",
    "  name: xtm-one-relay",
    "spec:",
    "  serviceName: xtm-one-relay",
    `  replicas: ${replicas}`,
    "  selector:",
    "    matchLabels: { app: xtm-one-relay }",
    "  template:",
    "    metadata:",
    "      labels: { app: xtm-one-relay }",
    "    spec:",
    "      securityContext: { runAsNonRoot: true, runAsUser: 65532, fsGroup: 65532 }",
    "      containers:",
    "        - name: relay",
    `          image: ${imageRef(input.version)}`,
    // A pinned build is never pulled again on a restart.
    ...(input.version?.trim() ? ["          imagePullPolicy: IfNotPresent"] : []),
    "          env:",
    ...env,
    "          readinessProbe:",
    "            httpGet: { path: /healthz, port: 8080 }",
    "          securityContext:",
    "            allowPrivilegeEscalation: false",
    "            readOnlyRootFilesystem: true",
    "            capabilities: { drop: [ALL] }",
    "          volumeMounts:",
    `            - { name: state, mountPath: ${RELAY_CONTAINER_STATE_DIR} }`,
    "  volumeClaimTemplates:",
    "    - metadata: { name: state }",
    "      spec:",
    "        accessModes: [ReadWriteOnce]",
    "        resources: { requests: { storage: 64Mi } }",
    "EOF",
  ].join("\n");
}

/** The command for `target`, or null when this deployment does not serve its installer. */
export function installCommand(target: RelayTarget, input: InstallCommandInput): string | null {
  switch (target) {
    case "linux":
    case "macos":
      return unixCommand(input);
    case "windows":
      return windowsCommand(input);
    case "docker":
      return dockerCommand(input);
    case "kubernetes":
      return kubernetesCommand(input);
  }
}

/**
 * The commands for a relay binary already on the machine (built from source, or unpacked from a release): enroll
 * once, then run it in the foreground with its allow-list (`relay.toml`, or `XTM_RELAY_ALLOWED_DESTINATIONS`). What a
 * deployment that serves no installer offers, so the key - shown once - is never lost.
 */
export function binaryCommand(target: RelayTarget, input: InstallCommandInput): string {
  const name = input.name?.trim();
  if (target === "windows") {
    return [
      `$env:XTM_RELAY_ALLOWED_DESTINATIONS = ${psQuote(destinationList(input.destinations))}`,
      `.\\xtm-one-relay.exe enroll --server-url ${psQuote(trimSlash(input.serverUrl))} --key ${psQuote(input.enrollmentKey)}${name ? ` --name ${psQuote(name)}` : ""}`,
      ".\\xtm-one-relay.exe run",
    ].join("\n");
  }
  return [
    `export XTM_RELAY_ALLOWED_DESTINATIONS=${shQuote(destinationList(input.destinations))}`,
    `xtm-one-relay enroll --server-url ${shQuote(trimSlash(input.serverUrl))} --key ${shQuote(input.enrollmentKey)}${name ? ` --name ${shQuote(name)}` : ""}`,
    "xtm-one-relay run",
  ].join("\n");
}

/** Whether a target enrolls several relays with one key (it needs a reusable key). */
export function targetNeedsReusableKey(target: RelayTarget, replicas = 1): boolean {
  return target === "kubernetes" && replicas > 1;
}
