// SPDX-License-Identifier: Apache-2.0

/**
 * The shared-context warning of the organization context (#11994): one text for
 * every surface that imports or syncs it from a platform (the import dialog, the
 * keep-in-sync step, the Syncs table) and, without the permission sentence, for
 * the surfaces that read nothing (the profile header, the CSV import).
 *
 * It is an info callout, not an error: nothing is refused, the administrator is
 * told what the data becomes once saved. It applies to the organization context
 * only; agent integrations and dashboards keep each user's own permissions.
 */
import type { ComponentProps } from "react";
import { Alert } from "@filigran/design-system";

import { useFormatter } from "@/i18n";

/** Whose permissions the import or sync reads with: the importer now, or the sync's owner. */
interface SharedContextActor {
  mode: "reads" | "runs";
  who: string;
  /** A sync saved by another administrator: saving it makes it run as this person (PUT /syncs/{id} re-owns it). */
  savingAs?: string | null;
}

interface OrganizationSharedContextNoticeProps {
  /** `source`: the three facts (read from a platform); `visibility`: the shared-visibility sentence only. */
  kind?: "source" | "visibility";
  actor?: SharedContextActor | null;
  className?: string;
  /** The Alert's own surface: 1 on a layer-2 host (a dialog), the DS default 2 elsewhere (DS Alert surface rule). */
  elevation?: ComponentProps<typeof Alert>["elevation"];
  /** Outbound (#1982 S4): XTM One is the source and the platform is written, so the text says what a push does. */
  direction?: "inbound" | "outbound";
  /** A module sync (#2650): it runs as the user picked in its run-as field, not as whoever sets it up. */
  runsAsPicked?: boolean;
}

export function OrganizationSharedContextNotice({ kind = "source", actor, className, elevation, direction = "inbound", runsAsPicked = false }: OrganizationSharedContextNoticeProps) {
  const { t_i18n } = useFormatter();
  if (direction === "outbound") {
    const runsAs = !actor
      ? null
      : actor.savingAs && actor.savingAs !== actor.who
        ? t_i18n("Writes as {who}. Saving makes it write as {me}.", { who: actor.who, me: actor.savingAs })
        : t_i18n("Writes as {who}", { who: actor.who });
    return (
      <Alert
        severity="info"
        elevation={elevation}
        className={className}
        data-testid="org-shared-context.outbound"
        title={t_i18n("Pushed from the organization context")}
        description={
          <span className="block space-y-1">
            <span className="block">
              {runsAsPicked
                ? t_i18n(
                    "XTM One rows are written to the target platform with the permissions of the user this sync runs as. Who can see them there follows that platform's own access rules, not XTM One's.",
                  )
                : t_i18n(
                    "XTM One rows are written to the target platform with the permissions of the person who sets up this sync. Who can see them there follows that platform's own access rules, not XTM One's.",
                  )}
            </span>
            {runsAs && (
              <span className="block font-medium" data-testid="org-shared-context.actor">
                {runsAs}
              </span>
            )}
          </span>
        }
      />
    );
  }
  const visibility = t_i18n("Once imported, the organization context is visible to every user and every agent of this XTM One platform.");
  const scope = t_i18n("This applies to the organization context only. Agent integrations, dashboards and every other feature keep each user's own permissions.");
  const permissions = runsAsPicked
    ? t_i18n(
        "Data is read from the source platform with the permissions of the user this sync runs as. The source platform's access rules, including organization segregation and markings, are not carried over.",
      )
    : t_i18n(
        "Data is read from the source platform with the permissions of the person who sets up this import or sync. The source platform's access rules, including organization segregation and markings, are not carried over.",
      );
  const who = !actor
    ? null
    : actor.mode === "reads"
      ? t_i18n("Reads as {who}", { who: actor.who })
      : actor.savingAs && actor.savingAs !== actor.who
        ? t_i18n("Runs as {who}. Saving makes it run as {me}.", { who: actor.who, me: actor.savingAs })
        : t_i18n("Runs as {who}", { who: actor.who });
  return (
    <Alert
      severity="info"
      elevation={elevation}
      className={className}
      data-testid={`org-shared-context.${kind}`}
      title={t_i18n("Shared organization context")}
      description={
        <span className="block space-y-1">
          <span className="block">{kind === "source" ? `${permissions} ${visibility}` : visibility}</span>
          <span className="block">{scope}</span>
          {who && (
            <span className="block font-medium" data-testid="org-shared-context.actor">
              {who}
            </span>
          )}
        </span>
      }
    />
  );
}
