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
 * Enroll a relay in a group: where it runs, what it may reach, then
 * one command to paste - and the dialog waits for the relay to connect.
 *
 * The enrollment key is minted when the commands are shown and returned once
 * by the API; it is single-use and short-lived unless a target needs it
 * reusable (a Kubernetes StatefulSet with several replicas).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, CheckCircle2, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProviderLogo } from "@/components/ui/provider-logo";
import { TagInput } from "@/components/ui/tag-input";
import {
  useCreateRelayEnrollmentKey, useGroupRelays, useRelayMeta,
} from "@/hooks/api/useRelayApi";
import { useAuthConfig } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import { platformOrigin } from "@/lib/base-path";
import { COPY_FEEDBACK_DELAY } from "@/lib/constants";
import { cn } from "@/lib/utils";
import type { RelayEnrollmentKeyCreated, RelayGroup } from "@/types";
import {
  binaryCommand,
  installCommand,
  targetNeedsReusableKey,
  type RelayTarget,
} from "./install-commands";
import { RELAY_LOGOS } from "./relay-labels";
import { Spinner } from "@filigran/design-system";

/** An absolute URL on the page origin when it targets this host (a TLS proxy may hand out http://). */
function onPageOrigin(url: string | null): string | null {
  if (!url) return null;
  try {
    const u = new URL(url, window.location.href);
    if (u.hostname !== window.location.hostname) return url;
    return `${window.location.origin}${u.pathname}${u.search}`;
  } catch {
    return url;
  }
}

export function CopyBlock({ text, multiline = false }: { text: string; multiline?: boolean }) {
  const { t_i18n } = useFormatter();
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-start gap-2">
      <pre
        className={cn(
          "flex-1 rounded-sm bg-elevation-hover px-3 py-2 font-mono-plex text-[11px] text-accent select-all leading-relaxed overflow-x-auto",
          multiline ? "whitespace-pre max-h-72" : "whitespace-pre-wrap break-all",
        )}
      >
        {text}
      </pre>
      <Button
        variant="ghost"
        size="icon"
        className="h-8 w-8 shrink-0"
        aria-label={t_i18n("Copy")}
        onClick={() => {
          void navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), COPY_FEEDBACK_DELAY);
        }}
      >
        {copied ? <Check className="h-4 w-4 text-feedback-success-primary" /> : <Copy className="h-4 w-4" />}
      </Button>
    </div>
  );
}

export function EnrollDialog({
  group,
  open,
  onOpenChange,
}: {
  group: RelayGroup;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t_i18n } = useFormatter();
  const { data: meta } = useRelayMeta({ enabled: open });
  const { data: authConfig } = useAuthConfig();
  const createKey = useCreateRelayEnrollmentKey(group.id);
  const { data: relays } = useGroupRelays(open ? group.id : undefined);

  const [target, setTarget] = useState<RelayTarget>("linux");
  const [name, setName] = useState("");
  const [destinations, setDestinations] = useState<string[]>(group.allowed_destinations ?? []);
  const [replicas, setReplicas] = useState(2);
  const [created, setCreated] = useState<RelayEnrollmentKeyCreated | null>(null);
  // The relays present when the key was minted: a new id afterwards is ours.
  const knownRelays = useRef<Set<string> | null>(null);

  useEffect(() => {
    if (!open) {
      setCreated(null);
      knownRelays.current = null;
      setName("");
      setDestinations(group.allowed_destinations ?? []);
    }
  }, [open, group.allowed_destinations]);

  const targetLabels: Record<RelayTarget, string> = {
    linux: t_i18n("Linux"),
    macos: t_i18n("macOS"),
    windows: t_i18n("Windows"),
    docker: t_i18n("Docker"),
    kubernetes: t_i18n("Kubernetes"),
  };

  const serverUrl = (authConfig?.base_url || meta?.server_url || platformOrigin()).replace(/\/+$/, "");
  const scripts = {
    sh: onPageOrigin(meta?.install_scripts?.sh ?? null),
    ps1: onPageOrigin(meta?.install_scripts?.ps1 ?? null),
  };
  const reusable = targetNeedsReusableKey(target, replicas);

  const command = useMemo(() => {
    if (!created) return null;
    return installCommand(target, {
      serverUrl,
      enrollmentKey: created.key,
      destinations,
      name: target === "kubernetes" ? undefined : name,
      scripts,
      version: meta?.relay_version,
      replicas,
    });
    // `scripts` is rebuilt every render from `meta`; its inputs are the deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [created, target, serverUrl, destinations, name, meta?.install_scripts?.sh, meta?.install_scripts?.ps1, meta?.relay_version, replicas]);

  const connected = useMemo(() => {
    if (!created || !relays || !knownRelays.current) return null;
    return relays.find((r) => !knownRelays.current!.has(r.id) && r.online) ?? null;
  }, [created, relays]);

  const mint = () => {
    knownRelays.current = new Set((relays ?? []).map((r) => r.id));
    createKey.mutate(
      {
        name: name.trim() || t_i18n("{target} relay", { target: targetLabels[target] }),
        max_uses: reusable ? null : 1,
        ttl_seconds: reusable ? 7 * 24 * 3600 : null,
      },
      { onSuccess: setCreated },
    );
  };

  const missingInstaller = created && command === null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{t_i18n("Enroll a relay in {group}", { group: group.name })}</DialogTitle>
          <DialogDescription>
            {t_i18n("Run the relay on a machine or in a container inside the network. It connects out to this platform over HTTPS: nothing is opened inbound.")}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5">
          <div className="space-y-2">
            <Label id="relay-enroll-target-label">{t_i18n("Where will it run?")}</Label>
            <div className="grid grid-cols-5 gap-2" role="group" aria-labelledby="relay-enroll-target-label">
              {(Object.keys(targetLabels) as RelayTarget[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  aria-pressed={target === t}
                  disabled={!!created}
                  onClick={() => setTarget(t)}
                  className={cn(
                    "flex flex-col items-center gap-2 rounded-sm border px-2 py-3 content-compact transition-colors",
                    target === t ? "border-accent/60 bg-accent/10 text-default-primary" : "border-elevation-subtle hover:bg-elevation-hover text-default-secondary",
                    created && target !== t && "opacity-40",
                  )}
                >
                  <ProviderLogo provider={t} logo={RELAY_LOGOS[t]} className="h-6 w-6" decorative />
                  {targetLabels[t]}
                </button>
              ))}
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            {target === "kubernetes" ? (
              <div className="space-y-1.5">
                <Label htmlFor="relay-enroll-replicas">{t_i18n("Replicas")}</Label>
                <Input id="relay-enroll-replicas" aria-label={t_i18n("Replicas")}
                  type="number"
                  min={1}
                  max={10}
                  value={replicas}
                  disabled={!!created}
                  onChange={(e) => setReplicas(Math.max(1, Math.min(10, Number(e.target.value) || 1)))}
                />
                <p className="text-[11px] text-default-secondary">
                  {t_i18n("Each replica enrolls as its own relay; the group balances requests across them.")}
                </p>
              </div>
            ) : (
              <div className="space-y-1.5">
                <Label htmlFor="relay-enroll-name">{t_i18n("Relay name (optional)")}</Label>
                <Input id="relay-enroll-name" aria-label={t_i18n("Relay name (optional)")}
                  value={name}
                  disabled={!!created}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t_i18n("The machine's hostname when empty")}
                />
              </div>
            )}
            <div className="space-y-1.5">
              <Label id="relay-enroll-destinations-label">{t_i18n("What may it reach?")}</Label>
              <TagInput
                aria-labelledby="relay-enroll-destinations-label"
                value={destinations}
                onChange={setDestinations}
                placeholder={t_i18n("splunk.corp.local:8089, 10.20.0.0/16:443")}
              />
              <p className="text-[11px] text-default-secondary">
                {t_i18n("The relay's own list, enforced on the machine. Anything else is refused, whatever the platform asks.")}
              </p>
            </div>
          </div>
          {destinations.length === 0 && (
            <p className="flex items-center gap-1.5 content-caption text-feedback-warning-primary">
              <AlertTriangle className="h-3.5 w-3.5" />
              {t_i18n("With an empty list the relay reaches nothing. Add the services it should carry requests to.")}
            </p>
          )}

          {!created ? (
            <div className="flex justify-end">
              <Button onClick={mint} disabled={createKey.isPending} aria-busy={createKey.isPending || undefined}>
                {createKey.isPending && <Spinner size="sm" tone="inherit" />}
                {t_i18n("Generate the install command")}
              </Button>
            </div>
          ) : (
            <div className="space-y-3 rounded-sm border border-accent/30 bg-accent/[0.03] p-4">
              <p className="content-compact font-medium">
                {target === "windows"
                  ? t_i18n("Run in an elevated PowerShell on the machine:")
                  : target === "docker"
                    ? t_i18n("Run on a Docker host inside the network:")
                    : target === "kubernetes"
                      ? t_i18n("Apply in a cluster inside the network:")
                      : t_i18n("Run on the machine (it installs a system service):")}
              </p>
              {missingInstaller ? (
                <>
                  <p className="content-caption text-feedback-warning-primary">
                    {t_i18n("This deployment does not serve the relay installers (a source build): with the relay binary on the machine (built from source or from a release), run:")}
                  </p>
                  <CopyBlock
                    text={binaryCommand(target, {
                      serverUrl,
                      enrollmentKey: created.key,
                      destinations,
                      name,
                      scripts,
                    })}
                    multiline
                  />
                </>
              ) : (
                <CopyBlock text={command ?? ""} multiline={target === "docker" || target === "kubernetes"} />
              )}
              <p className="text-[11px] text-default-secondary">
                {reusable
                  ? t_i18n("The key {prefix} can enroll several replicas for 7 days and is shown only once.", { prefix: created.key_prefix })
                  : t_i18n("The key {prefix} enrolls one relay, expires soon and is shown only once.", { prefix: created.key_prefix })}
              </p>
              <div
                className={cn(
                  "flex items-center gap-2 rounded-sm px-3 py-2 content-compact",
                  connected ? "bg-feedback-success-primary/10 text-feedback-success-primary" : "bg-elevation-hover text-default-secondary",
                )}
              >
                {connected ? (
                  <>
                    <CheckCircle2 className="h-4 w-4" />
                    {t_i18n("{name} is connected. You can route resources through {group} now.", { name: connected.name, group: group.name })}
                  </>
                ) : (
                  <>
                    <Spinner size="sm" tone="inherit" />
                    {t_i18n("Waiting for the relay to connect...")}
                  </>
                )}
              </div>
              <div className="flex justify-end">
                <Button variant={connected ? "default" : "ghost"} onClick={() => onOpenChange(false)}>
                  {connected ? t_i18n("Done") : t_i18n("Close")}
                </Button>
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
