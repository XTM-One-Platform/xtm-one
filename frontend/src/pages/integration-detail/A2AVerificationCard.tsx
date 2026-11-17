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

/** What an outbound A2A partner declares, and whether we can reach it.
 *
 * Rendered only for an integration whose direction is `outbound`. An inbound
 * one has no card to verify, and the read endpoint answers 404 for it — so
 * this card renders nothing rather than an empty shell.
 *
 * Two things it must never do, both learned from the spec rather than from
 * taste. It must not present an unverified card as verified: an unsigned card
 * is accepted, and saying so plainly is the mitigation for accepting it. And
 * it must not re-derive the health verdict — it shows the one the platform
 * wrote, so this view and the delegation path cannot disagree.
 */

// One glyph per axis, so a shape means the same thing everywhere in this
// card: Network is the SECTION, a shield is a target's VERDICT, and a file is
// the card's PROVENANCE. One shape carrying three meanings would leave the
// reader working out which is which from position.
import {
  Network,
  ShieldCheck,
  ShieldX,
  FileCheck2,
  FileWarning,
  FileQuestion,
  RefreshCw,
  AlertTriangle,
  Clock,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useFormatter } from "@/i18n";
import { useAiEnabled } from "@/hooks/usePlatformConfig";
import { formatShortDateTime } from "@/lib/utils";
import {
  type A2ATargetState,
  useA2AVerificationState,
  useVerifyA2APartner,
  useAcceptA2ACardChange,
} from "@/hooks/api/useA2AClientApi";
import type { Integration } from "@/types";

export function A2AVerificationCard({
  integration,
  canManage,
}: {
  integration: Integration;
  canManage: boolean;
}) {
  const { t_i18n } = useFormatter();
  const { data: state, isLoading, dataUpdatedAt } = useA2AVerificationState(integration.id);
  const verify = useVerifyA2APartner(integration.id);
  // Verifying reaches the partner, which the server refuses with AI off (A2A
  // follows agentic chat); the card is normally gone by then, but the button
  // states it rather than refusing on click (FR-018).
  const aiOff = !useAiEnabled();

  // 404 for an inbound registration, a non-A2A provider, or a deployment with
  // the outbound feature off. All three mean "nothing to show here".
  //
  // An INBOUND registration deliberately shows nothing here rather than an
  // empty card: its direction is visible on the Overview badge, and its
  // exposure state belongs to the agent's Channels & Bots tab. Two places
  // reporting one state is what this feature's whole control plane avoids.
  if (isLoading || !state) return null;

  const targets = state.targets ?? [];
  // Configured is not the same as reachable, and the header must not imply it
  // is. A partner with three agents where one has stopped answering is neither
  // healthy nor broken: a green shield over "3 agents" would report it as
  // working, which is the reading that sends an operator to the wrong place.
  const reachable = targets.filter((t) => t.usability === "usable").length;
  const allReachable = targets.length > 0 && reachable === targets.length;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Network className="h-4 w-4 text-accent" />
          {t_i18n("Partner verification")}
          {targets.length > 1 && (
            <Badge
              variant={allReachable ? "outline" : "destructive"}
              className="content-caption"
            >
              {allReachable
                ? t_i18n("{count, plural, one {# agent} other {# agents}}", { count: targets.length })
                : t_i18n("{reachable} of {total} reachable", {
                    reachable,
                    total: targets.length,
                  })}
            </Badge>
          )}
          {canManage && targets.length > 1 && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1 ml-auto"
              disabled={verify.isPending || aiOff}
              onClick={() => verify.mutate()}
            >
              <RefreshCw className="h-3 w-3" />
              {t_i18n("Verify all")}
            </Button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 content-compact">
        {targets.length === 0 ? (
          <p className="text-default-secondary">
            {t_i18n(
              "No partner agent is configured yet. Add one or more agent card URLs in the configuration above: each becomes its own delegation tool.",
            )}
          </p>
        ) : (
          targets.map((target, index) => (
            <div
              key={target.id}
              className={index > 0 ? "border-t border-elevation-subtle-soft pt-4" : undefined}
            >
              <TargetPanel
                integrationId={integration.id}
                target={target}
                canManage={canManage}
                asOf={dataUpdatedAt}
              />
            </div>
          ))
        )}
      </CardContent>
    </Card>
  );
}

/** One agent at the partner: what it declares, and whether we can reach it.
 *
 * Its own verify and accept buttons, because both decisions are about ONE
 * card. Verifying the whole partner to check one agent would re-open a
 * sibling's cooldown as a side effect of asking about something else, and
 * accepting "the card" of a partner exposing three would be a trust decision
 * the operator did not make about two of them.
 */
function TargetPanel({
  integrationId,
  target,
  canManage,
  asOf,
}: {
  integrationId: string;
  target: A2ATargetState;
  canManage: boolean;
  /** When `target` was fetched (epoch ms). */
  asOf: number;
}) {
  const { t_i18n } = useFormatter();
  const verify = useVerifyA2APartner(integrationId);
  const accept = useAcceptA2ACardChange(integrationId);
  const aiOff = !useAiEnabled();

  const usable = target.usability === "usable";
  // As `breaker.is_open` does: the column is cleared only by a success, so a
  // set value may be an expired cooldown. The fetch instant keeps the render pure.
  const paused =
    !!target.circuit_open_until && Date.parse(target.circuit_open_until) > asOf;
  // THREE states, not two. `null` means no card has been fetched yet — a
  // target discovered a moment ago, or one never verified — and it is neither
  // signed nor unsigned. It must never render as verified: that would claim a
  // signature was checked when nothing has been looked at (FR-017).
  const signature = target.signature_state ?? null;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        {usable ? (
          <ShieldCheck className="h-4 w-4 shrink-0 text-accent" />
        ) : (
          <ShieldX className="h-4 w-4 shrink-0 text-feedback-error-primary" />
        )}
        <span className="font-medium">
          {target.declared_name || target.card_url}
        </span>
        {target.agreed_protocol_version && (
          <Badge variant="outline" className="content-caption">
            A2A {target.agreed_protocol_version}
          </Badge>
        )}
        {/* The binding, shown only when it is NOT the one most partners use.
            A badge on every JSON-RPC registration would be noise on the
            common case; "HTTP+JSON" on a Google partner is the answer to a
            question an operator is about to ask. */}
        {target.transport && target.transport !== "JSONRPC" && (
          <Badge variant="outline" className="content-caption">
            {target.transport}
          </Badge>
        )}
      </div>

      {/* Card provenance, stated whichever way it went. Showing a badge only
          for the verified case would make silence read as the stronger
          claim, which is the opposite of what an unsigned card means. */}
      <div className="flex items-center gap-1.5">
        {signature === null ? (
          <span className="inline-flex items-center gap-1 text-default-secondary">
            <FileQuestion className="h-3.5 w-3.5 shrink-0" />
            {t_i18n("Not verified yet")}
          </span>
        ) : signature === "unsigned" ? (
          <Tip
            content={t_i18n(
              "This partner does not sign its agent card. It is trusted on the encrypted connection and the URL you supplied: the same basis on which partners trust this platform's own unsigned cards. A signature that failed to verify would make the registration unusable instead.",
            )}
          >
            <span className="inline-flex items-center gap-1 text-feedback-warning-primary">
              <FileWarning className="h-3.5 w-3.5 shrink-0" />
              {t_i18n("Card is unsigned")}
            </span>
          </Tip>
        ) : (
          <span className="inline-flex items-center gap-1 text-accent">
            <FileCheck2 className="h-3.5 w-3.5 shrink-0" />
            {t_i18n("Card signature verified")}
          </span>
        )}
      </div>

      {/* What the partner says this agent is. Shown before anyone binds it. */}
      {target.declared_description && (
        <p className="text-default-secondary">{target.declared_description}</p>
      )}
      {(target.declared_skills?.length ?? 0) > 0 && (
        <div className="flex flex-wrap gap-1">
          {target.declared_skills!.map((skill) => (
            <Badge key={skill} variant="outline" className="content-caption">
              {skill}
            </Badge>
          ))}
        </div>
      )}
      {/* Declared content types, shown before the registration is put to use
          (FR-018): a content-type mismatch refuses the delegation, so the
          operator should see what the card declares here, not in a refusal. */}
      {((target.declared_input_types?.length ?? 0) > 0 ||
        (target.declared_output_types?.length ?? 0) > 0) && (
        <p className="content-caption text-default-secondary">
          {(target.declared_input_types?.length ?? 0) > 0 &&
            `${t_i18n("Accepts")}: ${target.declared_input_types!.join(", ")}`}
          {(target.declared_input_types?.length ?? 0) > 0 &&
            (target.declared_output_types?.length ?? 0) > 0 &&
            " · "}
          {(target.declared_output_types?.length ?? 0) > 0 &&
            `${t_i18n("Produces")}: ${target.declared_output_types!.join(", ")}`}
        </p>
      )}

      {/* How this partner can be waited on. Shown for the same reason as the
          content types: an operator asking why a delegation waited the way it
          did should find the answer on the card, not infer it. */}
      {target.declares_callbacks && (
        <p className="content-caption text-default-secondary">
          {t_i18n("Supports")}: {t_i18n("callbacks")}
        </p>
      )}

      {!usable && (
        <div className="flex items-start gap-1.5 rounded-sm bg-feedback-error-primary/10 p-2 text-feedback-error-primary">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          {/* A freshly discovered target is `unusable` with NO reason — the
              row exists before any card has been fetched. Falling back to
              `never_verified` is what turns a bare red cross into something an
              operator can act on. */}
          <span>{reasonText(target.unusable_reason || "never_verified", t_i18n)}</span>
        </div>
      )}

      {paused && (
        <div className="flex items-start gap-1.5 rounded-sm bg-elevation-hover p-2">
          <Clock className="h-3.5 w-3.5 shrink-0 mt-0.5 text-default-secondary" />
          <span className="text-default-secondary">
            {t_i18n(
              "Paused after repeated failures. Verifying now retries immediately.",
            )}
          </span>
        </div>
      )}

      {/* Both endpoints load with ``manage=True``, so for a viewer these can
          only produce a 403 toast. The panel itself stays — what it SHOWS is
          the partner's cards, which sight alone earns. */}
      <div className="flex items-center gap-2">
        {canManage && (
        <Button
          size="sm"
          variant="outline"
          className="h-7 gap-1"
          disabled={verify.isPending || aiOff}
          onClick={() => verify.mutate(target.id)}
        >
          <RefreshCw className="h-3 w-3" />
          {t_i18n("Verify now")}
        </Button>
        )}
        {/* Accepting sends back the fingerprint the operator was SHOWN, for
            THIS card. The server refuses any other, so a card that moved
            again between reading and clicking cannot be accepted unread. */}
        {canManage && target.card_change_pending && target.card_fingerprint && (
          <Button
            size="sm"
            className="h-7"
            disabled={accept.isPending || aiOff}
            onClick={() =>
              accept.mutate({
                target_id: target.id,
                fingerprint: target.card_fingerprint!,
              })
            }
          >
            {t_i18n("Accept card change")}
          </Button>
        )}
      </div>

      {target.last_verified_at && (
        <p className="content-caption text-default-secondary">
          {t_i18n("Last verified")}: {formatShortDateTime(target.last_verified_at)}
        </p>
      )}
    </div>
  );
}

/** What an operator should DO about each reason.
 *
 * Mirrors the backend probe's table on purpose: an operator may meet a reason
 * here or in "Test Connection", and two different explanations of one state
 * would send them in two directions.
 */
function reasonText(reason: string, t: (s: string) => string): string {
  const map: Record<string, string> = {
    never_verified: t("Not verified yet: use Verify now."),
    card_unfetchable: t("The card URL did not answer. Check the URL and that the partner is reachable."),
    card_malformed: t("The card URL answered with something that is not an agent card."),
    transport_unverified: t("The card URL must be https on a connection we can verify."),
    signature_invalid: t("The partner's card signature does not verify. This cannot be overridden."),
    no_common_protocol_version: t("The partner speaks no A2A version this platform supports."),
    no_common_transport: t("The partner serves no A2A transport this platform speaks: it offers gRPC only, or a binding we do not recognise."),
    request_rejected_by_partner: t("The partner rejected the request we sent. Not a card problem: the server log carries the partner's own words. Use Verify now to clear this once it is fixed."),
    credential_cannot_mint_token: t("The stored credentials cannot produce a token. Re-enter them."),
    credential_rejected_by_partner: t("The partner rejected our token. Check the credentials with them."),
    destination_not_permitted: t("That destination is not one this platform will send to."),
    destination_blocked_by_deployment: t("This deployment's network policy refused the partner's address."),
    origin_mismatch: t("The partner's card points at a different origin than its card URL."),
    card_change_unaccepted: t("The partner changed its card. Review and accept the change to resume."),
    signature_downgraded: t("The partner stopped signing its card. Review and accept to resume."),
    protocol_version_downgraded: t("The partner stopped serving the agreed protocol version."),
  };
  return map[reason] ?? reason;
}
