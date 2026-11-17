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
 * Tool approval for connected Suite platforms.
 *
 * A registered platform contributes tools XTM One cannot classify — they are
 * not on the shipped whitelist and never can be, because we do not know what
 * they do. So they ask for approval by default, and on upgrade that starts
 * happening everywhere at once.
 *
 * Whether that is right turns on two different questions, which is why this
 * screen shows two things per platform rather than one toggle:
 *
 *   Can it ask?    A fact the platform reports about itself. Only it knows.
 *   If not, then?  A policy only an operator can set.
 *
 * The list is built from actual registrations, so a platform we have never
 * heard of appears here the moment it connects, with no code change.
 */

import { Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { HitlIcon } from "@/components/ui/hitl-icon";
import { hitlState } from "@/lib/hitl";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/components/ui/toast";
import { useFormatter } from "@/i18n";
import {
  usePlatformRegistrations,
  useSetPlatformApproval,
  type PlatformRegistration,
} from "@/hooks/api/usePlatformApi";
import { Spinner } from "@filigran/design-system";

/** Why a platform is in its current state, in words an operator can act on. */
function useProvenance() {
  const { t_i18n } = useFormatter();
  return (reg: PlatformRegistration): string => {
    if (reg.approval_policy_set_by === "admin") {
      return t_i18n("Set by an administrator");
    }
    if (reg.approval_policy_set_by === "deployment") {
      return t_i18n("From this deployment's configuration");
    }
    return t_i18n("Default");
  };
}

function PlatformRow({ reg }: { reg: PlatformRegistration }) {
  const { t_i18n } = useFormatter();
  const { toast } = useToast();
  const provenance = useProvenance();
  const setApproval = useSetPlatformApproval();

  const apply = (policy: "require" | "skip") => {
    setApproval.mutate(
      { id: reg.id, policy },
      {
        onSuccess: () =>
          toast({
            title:
              policy === "require"
                ? t_i18n("{platform} changes now ask for approval", {
                    platform: reg.platform_title,
                  })
                : t_i18n("{platform} tools now run without approval", {
                    platform: reg.platform_title,
                  }),
          }),
        onError: () =>
          toast({
            title: t_i18n("Could not change the approval setting"),
            variant: "error",
          }),
      },
    );
  };

  return (
    <div className="rounded-sm border p-4 space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <HitlIcon state={hitlState(reg.gates_tools)} className="size-4" />
            <span className="font-medium truncate">{reg.platform_title}</span>
            <span className="content-compact text-default-secondary shrink-0">
              v{reg.platform_version}
            </span>
          </div>
          <p className="content-compact text-default-secondary mt-1">
            {reg.gates_tools
              ? t_i18n(
                  "Anything that changes this platform asks for approval; its read tools run without asking.",
                )
              : t_i18n("Its tools run without asking.")}{" "}
            {provenance(reg)}
            {". "}
            {reg.supports_approval_prompts === true
              ? t_i18n("This platform can display approval prompts.")
              : reg.supports_approval_prompts === false
                ? t_i18n("This platform cannot display approval prompts.")
                : t_i18n(
                    "This platform has not said whether it can display approval prompts.",
                  )}
          </p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {setApproval.isPending && (
            <Spinner size="sm" tone="inherit" className="text-default-secondary" />
          )}
          <Switch
            checked={reg.gates_tools}
            disabled={setApproval.isPending}
            onCheckedChange={(on) => apply(on ? "require" : "skip")}
            aria-label={t_i18n("Require approval to change {platform}", {
              platform: reg.platform_title,
            })}
          />
        </div>
      </div>

      {/*
        The one case where "never override a human" and "fail safe" pull
        against each other: approval was switched off because the platform
        could not ask, and now something can. Doing either silently would be
        wrong, so the choice stands and the safer posture is one click away.

        Two kinds of evidence, worded apart on purpose. A declaration is the
        platform speaking for its assistant. An observation is one client of it
        that answers prompts — enough to raise the question, not enough to
        claim the platform is capable, because an unattended caller of the same
        platform may still send nothing.
      */}
      {reg.capability_nudge && (
        <div className="flex items-start gap-2 rounded-sm bg-elevation-highlight p-3">
          <Sparkles className="size-4 mt-0.5 shrink-0 text-accent" />
          <div className="flex-1 min-w-0">
            <p className="content-compact">
              {reg.capability_nudge_reason === "observed"
                ? t_i18n(
                    "A client of {platform} says it can display approval prompts. Changes to it are still set to run without asking.",
                    { platform: reg.platform_title },
                  )
                : t_i18n(
                    "{platform} can now display approval prompts. Changes to it are still set to run without asking.",
                    { platform: reg.platform_title },
                  )}
            </p>
          </div>
          <Button
            size="sm"
            variant="outline"
            disabled={setApproval.isPending}
            onClick={() => apply("require")}
          >
            {t_i18n("Require approval")}
          </Button>
        </div>
      )}
    </div>
  );
}

export function PlatformApprovalSection() {
  const { t_i18n } = useFormatter();
  const { data: registrations, isLoading } = usePlatformRegistrations();

  // Unregistering keeps the row and only sets the status, and a platform in
  // that state governs nothing: `platform_approval_by_provider` filters it out,
  // so its providers fall back to their own per-integration control. Offering a
  // switch here would write a policy the gate never reads, and show it as
  // changed. Excluding exactly the one terminal status, as the server does —
  // an unrecognised status keeps governing, so it keeps its control too.
  const connected = (registrations ?? []).filter(
    (reg) => reg.status !== "unregistered",
  );

  // Nothing connected means nothing to decide — an empty card here would only
  // raise a question the operator does not have.
  if (!isLoading && !connected.length) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t_i18n("Approval for connected platforms")}</CardTitle>
        <CardDescription>
          {t_i18n(
            "A connected platform's read-only tools run without asking. Everything that changes the platform asks first, on an agent that asks before running tools, including its consolidated management tools, which can list or delete depending on how they are called. Turn that off for a platform whose assistant cannot yet display an approval prompt.",
          )}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <Spinner size="sm" tone="inherit" className="text-default-secondary" label={t_i18n("Loading...")} />
        ) : (
          connected.map((reg) => <PlatformRow key={reg.id} reg={reg} />)
        )}
      </CardContent>
    </Card>
  );
}
