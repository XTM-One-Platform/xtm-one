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

/** A2A protocol surface for an inbound A2A integration.
 *
 * Rendered for both A2A providers, except `A2ACredentialCard`, which belongs
 * to `oauth_a2a` alone — Foundry A2A stores pinned facts rather than a
 * credential, edited through the ordinary configuration card.
 *
 * - `A2AExposedCard` and `A2AToolsCard` take the place of the generic
 *   "Available Tools" card, because A2A answers as two questions what one
 *   card asks: what a partner may invoke HERE, and what this binding gives
 *   YOUR agents. Conflating them is the mistake the generic card makes on
 *   this provider — its promise ("automatically available to any agent that
 *   has this integration bound") and its per-tool Test button are both wrong
 *   for a method only a partner calls.
 * - Direction decides each panel's content, and the two are not symmetric:
 *   inbound serves the task surface and confers one tool, outbound serves
 *   one callback route and confers one tool per verified partner agent.
 * - `A2AUsageCard` is the inbound usage narrative: what a partner does with
 *   those operations end to end, and what the surface refuses.
 *
 * Every list comes from the backend — the catalog for what the protocol
 * admits, the registration state for the tool names an agent actually sees —
 * so the UI cannot show a name or an operation the platform does not serve.
 */

import { useState } from "react";
import {
  Wrench, BookOpen, Pencil, Info, Radio, ShieldX, ShieldAlert, ShieldCheck, KeyRound, AlertTriangle, Copy, Check,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { Tip } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { useFormatter } from "@/i18n";
import {
  OAUTH_A2A_PROVIDER, useA2ALastRefusal, useA2APartnerActivity, useRegenerateA2ACredential,
} from "@/hooks/api/useA2AApi";
import { formatShortDateTimeIntl, formatTimeAgo } from "@/lib/relative-time";
import { useA2ADirection, useA2AVerificationState } from "@/hooks/api/useA2AClientApi";
import { missingCardCount, useBoundAgentCards } from "./a2a-card-rows";
import type { A2ATargetState } from "@/hooks/api/useA2AClientApi";
import type { Integration, ProviderCatalogItem, ProviderTool } from "@/types";

/** One row per thing, shared by both panels below.
 *
 * The columns are the same question either way — what is it called, what does
 * it do, does it change anything — so a reader compares the two panels rather
 * than learning two layouts.
 */
function SurfaceTable({ rows, nameHeader }: { rows: ProviderTool[]; nameHeader: string }) {
  const { t_i18n } = useFormatter();
  return (
    <div className="rounded-sm border border-elevation-subtle-soft overflow-hidden">
      <div className="grid grid-cols-[200px_1fr_80px] gap-4 px-4 py-2 bg-elevation-hover border-b border-elevation-subtle-soft">
        <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{nameHeader}</p>
        <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Description")}</p>
        <p className="content-caption font-semibold uppercase tracking-wider text-default-secondary">{t_i18n("Type")}</p>
      </div>
      {rows.map((op, idx) => {
        const isWrite = op.tool_type === "write";
        return (
          <div
            key={op.name}
            className={cn(
              "grid grid-cols-[200px_1fr_80px] gap-4 px-4 py-2.5 items-center",
              idx < rows.length - 1 ? "border-b border-elevation-subtle-soft" : "",
            )}
          >
            <Tip content={op.name}>
              <code className={cn(
                "rounded-sm px-1.5 py-0.5 text-[11px] font-mono-plex truncate block max-w-[200px]",
                isWrite ? "bg-feedback-alert-primary/10 text-feedback-warning-tertiary" : "bg-feedback-success-primary/10 text-feedback-success-primary",
              )}>
                {op.name}
              </code>
            </Tip>
            <span className="content-compact text-default-secondary leading-relaxed">{op.description}</span>
            <Badge variant="outline" className="text-[9px] w-fit">
              {isWrite ? <Pencil className="h-2.5 w-2.5 mr-0.5 text-feedback-warning-primary" /> : <BookOpen className="h-2.5 w-2.5 mr-0.5 text-feedback-success-primary" />}
              {isWrite ? t_i18n("Write") : t_i18n("Read")}
            </Badge>
          </div>
        );
      })}
    </div>
  );
}

/** What a partner may invoke on this deployment through this registration.
 *
 * Direction decides the whole content, because the two directions expose
 * genuinely different things: inbound serves the task surface, outbound
 * serves one callback route and only where an operator allows it.
 */
export function A2AExposedCard({
  integration,
  catalogEntry,
}: {
  integration: Integration;
  catalogEntry: ProviderCatalogItem;
}) {
  const { t_i18n } = useFormatter();
  const direction = useA2ADirection(integration);
  const { data: state } = useA2AVerificationState(
    direction === "outbound" ? integration.id : "",
  );

  const inbound = direction === "inbound";
  const rows = inbound
    ? catalogEntry.available_tools ?? []
    : catalogEntry.a2a_outbound_exposed ?? [];
  if (rows.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Radio className="h-4 w-4 text-accent" />
          {t_i18n("What this exposes")}
          {!inbound && (
            <Badge
              variant={state?.callbacks_reachable ? "cyan" : "outline"}
              className="content-caption"
            >
              {state?.callbacks_reachable
                ? t_i18n("Reachable")
                : t_i18n("Not reachable")}
            </Badge>
          )}
          <Badge variant="outline" className="content-caption ml-auto">
            {t_i18n("{count} endpoints", { count: rows.length })}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-sm bg-elevation-hover p-3">
          <div className="flex items-start gap-2">
            <Info className="h-3.5 w-3.5 shrink-0 text-accent mt-0.5" />
            <p className="content-compact text-default-secondary leading-relaxed">
              {inbound
                ? t_i18n(
                    "What a federated partner may invoke on an agent this configuration is bound to. Every other JSON-RPC method is refused.",
                  )
                : state?.callbacks_reachable
                  ? t_i18n(
                      "What a partner may call back on when work you delegated finishes. Registering one tells the partner where this deployment lives, which in an outbound-only relationship it would otherwise never learn.",
                    )
                  : t_i18n(
                      "Nothing is exposed: this deployment is not reachable, so no callback is registered and every delegation is followed by asking. Set A2A_CLIENT_CALLBACK_BASE_URL, or A2A_CLIENT_CALLBACKS_REACHABLE to refuse deliberately.",
                    )}
            </p>
          </div>
        </div>
        <SurfaceTable rows={rows} nameHeader={inbound ? t_i18n("Method") : t_i18n("Endpoint")} />
      </CardContent>
    </Card>
  );
}

/** What this registration gives the agents bound to it.
 *
 * The same question every other integration's tool card answers, so an
 * operator reads A2A the way they read the rest. Inbound confers one tool and
 * outbound one per verified partner agent — a model choosing between partners
 * can only do so if their names differ, so the names are what is shown.
 */
export function A2AToolsCard({
  integration,
  catalogEntry,
}: {
  integration: Integration;
  catalogEntry: ProviderCatalogItem;
}) {
  const { t_i18n } = useFormatter();
  const direction = useA2ADirection(integration);
  const { data: state } = useA2AVerificationState(
    direction === "outbound" ? integration.id : "",
  );

  const inbound = direction === "inbound";
  const rows: ProviderTool[] = inbound
    ? catalogEntry.a2a_inbound_agent_tools ?? []
    : [
        ...(state?.targets ?? [])
          .filter((t: A2ATargetState) => t.tool_name)
          .map((t: A2ATargetState) => ({
            name: t.tool_name as string,
            description: t.declared_description
              || t_i18n("Delegate one unit of work to {name}.", { name: t.declared_name ?? t.card_url }),
            tool_type: "write" as const,
          })),
        // One per registration, not per agent: it takes the task id a
        // hand-back gave the agent, so it needs no target to pick.
        ...(state?.cancel_tool_name
          ? [{
              name: state.cancel_tool_name,
              description: t_i18n(
                "Stop a task still running at this partner. The agent offers this when a delegation hands back, and uses it only when the user asks.",
              ),
              tool_type: "write" as const,
            }]
          : []),
      ];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Wrench className="h-4 w-4 text-accent" />
          {t_i18n("What this brings your agents")}
          <Badge variant="outline" className="content-caption ml-auto">
            {t_i18n("{count} tools", { count: rows.length })}
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-sm bg-elevation-hover p-3">
          <div className="flex items-start gap-2">
            <Info className="h-3.5 w-3.5 shrink-0 text-accent mt-0.5" />
            <p className="content-compact text-default-secondary leading-relaxed">
              {inbound
                ? t_i18n(
                    "Offered only while an external task is running: there is no caller to ask in an ordinary conversation. A2A blocks a task on its calling agent, so the question is answered by a machine and never waits for a person.",
                  )
                : t_i18n(
                    "One per verified agent at this partner, named so a model can choose between them. Each submits, follows and concludes one unit of work; cancellation and callbacks are its internals, not separate tools.",
                  )}
            </p>
          </div>
        </div>
        {rows.length > 0 ? (
          <SurfaceTable rows={rows} nameHeader={t_i18n("Tool")} />
        ) : (
          <p className="content-compact text-default-secondary">
            {t_i18n("None yet: no agent at this partner has verified.")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

export function A2AUsageCard() {
  const { t_i18n } = useFormatter();

  const steps: { title: string; body: string }[] = [
    {
      title: t_i18n("1. Discover the agent"),
      body: t_i18n(
        "The partner fetches the agent card URL (shown on the agent's Channels & Bots tab once this integration is bound to it). The card is served anonymously and declares the agent's skills, the JSON-RPC endpoint and the accepted credentials.",
      ),
    },
    {
      title: t_i18n("2. Authenticate"),
      body: t_i18n(
        "Azure mints a token for the partner's own identity and refreshes it forever. Every request is verified against this configuration's pins (tenant signature, exact audience, allowed caller), with nothing cached, so an edit here applies to the next request.",
      ),
    },
    {
      title: t_i18n("3. Submit and poll"),
      body: t_i18n(
        "message/send starts a run and returns a task id; the partner then polls tasks/get until the task reaches a terminal state and reads the answer from its artifacts. tasks/cancel stops a run that is no longer wanted.",
      ),
    },
  ];

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          <Radio className="h-4 w-4 text-accent" />
          {t_i18n("How partners use it")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-3">
          {steps.map((step) => (
            <div key={step.title} className="rounded-sm bg-elevation-hover p-3">
              <p className="content-compact font-medium">{step.title}</p>
              <p className="text-[11px] text-default-secondary leading-relaxed mt-1">{step.body}</p>
            </div>
          ))}
        </div>

        <div className="space-y-1.5 rounded-sm border border-elevation-subtle-soft p-3">
          <div className="flex items-center gap-1.5 text-[11px] font-medium">
            <ShieldX className="h-3.5 w-3.5 text-accent" />
            {t_i18n("Bounds enforced on every request")}
          </div>
          <ul className="space-y-1 text-[11px] text-default-secondary list-disc pl-4">
            <li>
              {t_i18n(
                "Work runs as the agent's exposure run-as user and sees exactly what that user sees: partners get no access of their own.",
              )}
            </li>
            <li className="text-feedback-warning-primary">
              {t_i18n(
                "Binding an agent here gives this partner that agent's whole tool set, and its handover peers', with no per-partner narrowing. The run-as identity bounds whose data is reached, not what may be done with it.",
              )}
            </li>
            <li>
              {t_i18n(
                "Submissions are rate-limited per partner registration (10/min by default) and polling far higher (1800/min); both credential forms share one budget.",
              )}
            </li>
            <li>
              {t_i18n(
                "Streaming and task listing are not served: they are refused by name so a partner falls back to polling instead of retrying blindly. Push notifications are served, so a partner may register a callback instead of polling.",
              )}
            </li>
            <li>
              {t_i18n(
                "Every refusal looks identical from outside: an unknown task, another partner's task, a revoked exposure and a bad credential are indistinguishable. Causes appear in the server log only.",
              )}
            </li>
          </ul>
        </div>
      </CardContent>
    </Card>
  );
}


/** The partner credential an INBOUND OAuth A2A integration carries.
 *
 * The client id is an identifier the partner sends in clear on every token
 * exchange, so it re-displays freely. The SECRET is stored only as a bcrypt
 * hash and is therefore returned exactly once, by the mint itself — there is
 * no re-display, only regeneration, which retires the previous secret on
 * every agent this integration is bound to.
 *
 * Its own section rather than a field in the configuration card, matching the
 * runner keys in ``desktop/RunnersSection``: a minted secret shown once is a
 * lifecycle action, not a value an operator supplies, and an edit form whose
 * idiom is "type to replace what is stored" cannot express it.
 *
 * Gates ITSELF on direction, like every other card in the Config tab. An
 * OUTBOUND registration presents a credential the partner issued and has no
 * business here: minting writes a client id into ``extra_config``, which is
 * exactly where the partner-issued one lives, so a stray render of this card
 * offers a button that replaces a working credential with a meaningless one.
 * The API refuses it too, but the button should never be there to press.
 */
export function A2ACredentialCard({
  integration,
  canManage,
}: {
  integration: Integration;
  canManage: boolean;
}) {
  const { t_i18n, intl } = useFormatter();
  const direction = useA2ADirection(integration);
  const regenerate = useRegenerateA2ACredential(integration.id);
  // The page finds the integration in the list, whose rows carry no
  // a2a_credential: the refusal comes from the single read, managers only.
  const { data: refused } = useA2ALastRefusal(integration.id, direction === "inbound" && canManage);
  // Read from the bound agents' exposure status, built by the same helper as
  // the card's tokenUrl: the deployment's public origin, never a UI constant.
  const { tokenUrl } = useBoundAgentCards(integration.id);
  const [issued, setIssued] = useState<{ client_id: string; client_secret: string } | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [ConfirmEl, confirm] = useConfirm();

  // Minted when the integration is created and kept in extra_config, which is
  // where every non-secret provider setting lives.
  const storedClientId =
    typeof integration.extra_config?.client_id === "string"
      ? (integration.extra_config.client_id as string)
      : null;
  const clientId = issued?.client_id ?? storedClientId;

  const copy = (value: string, key: string) => {
    navigator.clipboard.writeText(value);
    setCopied(key);
    setTimeout(() => setCopied(null), 2000);
  };

  // A definite "inbound" only: an errored or pending direction must not
  // render a mint button to an outbound registration.
  if (direction !== "inbound") return null;

  // A regenerate rotates the SECRET and keeps the client id the partner
  // already holds (#105): the partner pastes one value. Changing both is its
  // own action, whose confirm says both change.
  const onRegenerate = async () => {
    if (
      clientId &&
      !(await confirm({
        title: t_i18n("Regenerate the secret?"),
        description: t_i18n(
          "The client ID stays the same. The current secret stops working immediately, on every agent this integration is bound to. The partner must paste the new secret.",
        ),
        confirmLabel: t_i18n("Regenerate secret"),
        variant: "warning",
      }))
    ) {
      return;
    }
    regenerate.mutate(undefined, { onSuccess: (data) => setIssued(data) });
  };

  const onRotateClientId = async () => {
    if (
      !(await confirm({
        title: t_i18n("Rotate the client ID and the secret?"),
        description: t_i18n(
          "Both change. The current client ID and secret stop working immediately, on every agent this integration is bound to. The partner must paste the new client ID and the new secret.",
        ),
        confirmLabel: t_i18n("Rotate both"),
        variant: "warning",
      }))
    ) {
      return;
    }
    regenerate.mutate({ rotateClientId: true }, { onSuccess: (data) => setIssued(data) });
  };

  const refusalLabel = refused ? refusalReasonLabel(refused.reason, t_i18n) : null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between content-base">
          <span className="flex items-center gap-2">
            <KeyRound className="h-4 w-4" />
            {t_i18n("Partner credential")}
          </span>
          {canManage && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 content-caption"
              onClick={onRegenerate}
              disabled={regenerate.isPending}
            >
              {regenerate.isPending
                ? t_i18n("Generating...")
                : clientId
                  ? t_i18n("Regenerate secret")
                  : t_i18n("Generate credential")}
            </Button>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-[11px] text-default-secondary">
          {t_i18n(
            "The partner exchanges these at the token URL on the agent card for a short-lived bearer token. One credential per partner: it works on every agent this integration is bound to. Regenerating the secret keeps the client ID.",
          )}
        </p>

        {issued ? null : clientId ? (
          <CopyRow
            label={t_i18n("Client ID")}
            value={clientId}
            copied={copied === "id"}
            onCopy={() => copy(clientId, "id")}
          />
        ) : (
          <p className="content-caption text-default-secondary">
            {t_i18n("No credential yet: generate one so the partner has something to authenticate with.")}
          </p>
        )}

        {tokenUrl ? (
          <div className="space-y-1">
            <CopyRow
              label={t_i18n("Token URL")}
              value={tokenUrl}
              copied={copied === "token"}
              onCopy={() => copy(tokenUrl, "token")}
            />
            <p className="content-caption text-default-secondary">
              {t_i18n("The partner pastes it into Partner token endpoint.")}
            </p>
          </div>
        ) : (
          <p className="content-caption text-default-secondary">
            {t_i18n("The token URL appears once an agent bound to this integration publishes its card.")}
          </p>
        )}

        {issued ? (
          <div className="space-y-2 rounded-sm border border-feedback-alert-primary/40 bg-feedback-alert-primary/5 p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5 content-caption font-medium text-feedback-warning-primary">
                <AlertTriangle className="h-3.5 w-3.5" />
                {t_i18n("Copy this now: the secret is never shown again")}
              </div>
              <Button
                size="sm"
                variant="outline"
                className="h-7 content-caption"
                data-testid="a2a-credential-copy-both"
                onClick={() => copy(`client_id=${issued.client_id}\nclient_secret=${issued.client_secret}`, "both")}
              >
                {copied === "both" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                {t_i18n("Copy both")}
              </Button>
            </div>
            <CopyRow
              label={t_i18n("Client ID")}
              value={issued.client_id}
              copied={copied === "issued-id"}
              onCopy={() => copy(issued.client_id, "issued-id")}
            />
            <CopyRow
              label={t_i18n("Client secret")}
              value={issued.client_secret}
              copied={copied === "secret"}
              onCopy={() => copy(issued.client_secret, "secret")}
            />
            <p className="content-caption text-default-secondary">
              {t_i18n("The partner pastes the client ID and the secret together, as one pair.")}
            </p>
          </div>
        ) : (
          <p className="content-caption text-default-secondary">
            {t_i18n("The secret is shown only once, when it is generated. If it is lost, regenerate it: the client ID stays, the old secret stops working immediately.")}
          </p>
        )}
        {refused && (
          <div className="space-y-1 rounded-sm border border-line-primary p-3" data-testid="a2a-credential-last-refusal">
            <div className="content-caption font-medium">{t_i18n("Last refused token request")}</div>
            <p className="content-caption text-default-secondary">
              <Tip content={formatTimeAgo(intl, refused.at)}>
                <span data-testid="a2a-credential-last-refusal-at">{formatShortDateTimeIntl(intl, refused.at)}</span>
              </Tip>
              {" · "}
              {t_i18n("Client ID")} {refused.client_id ?? "-"}
            </p>
            {refusalLabel ? (
              <>
                <p className="content-caption font-medium">{refusalLabel.reason}</p>
                <p className="content-caption">{refusalLabel.whatToDo}</p>
              </>
            ) : (
              refused.what_to_do && <p className="content-caption">{refused.what_to_do}</p>
            )}
          </div>
        )}
        {canManage && clientId && (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 content-caption"
            onClick={onRotateClientId}
            disabled={regenerate.isPending}
          >
            {t_i18n("Rotate client ID and secret")}
          </Button>
        )}
        {regenerate.isError && (
          <p className="content-caption text-feedback-error-primary">{regenerate.error.message}</p>
        )}
      </CardContent>
      {ConfirmEl}
    </Card>
  );
}

/** The reason of a refused token request and what the operator does about it,
 * translated here; null for a code this UI does not know, which then shows the
 * backend's English sentence (`what_to_do`) as a fallback. */
function refusalReasonLabel(
  reason: string | null,
  t_i18n: (key: string) => string,
): { reason: string; whatToDo: string } | null {
  switch (reason) {
    case "secret_mismatch":
      return {
        reason: t_i18n("Wrong secret for the current client ID"),
        whatToDo: t_i18n("The partner must paste the latest secret."),
      };
    case "unknown_client":
      return {
        reason: t_i18n("Client ID rotated away"),
        whatToDo: t_i18n("The partner must paste the current client ID and secret."),
      };
    case "no_reachable_agent":
      return {
        reason: t_i18n("No agent exposed"),
        whatToDo: t_i18n("The credential is valid, but no agent bound to this integration is exposed."),
      };
    default:
      return null;
  }
}

function CopyRow({
  label,
  value,
  copied,
  onCopy,
}: {
  label: string;
  value: string;
  copied: boolean;
  onCopy: () => void;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-1">
      <div className="content-caption uppercase tracking-wide text-default-secondary">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded-sm bg-elevation-highlight px-2 py-1 content-caption">{value}</code>
        <Button size="sm" variant="outline" className="h-7 gap-1" aria-label={t_i18n("Copy")} onClick={onCopy}>
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
        </Button>
      </div>
    </div>
  );
}


/** Whether an INBOUND partner can currently reach us, and what is stopping it.
 *
 * The mirror of `A2AVerificationCard`, which answers the outbound question
 * ("can we reach the partner, and is its card still what we verified").
 * Without this card, a registration with nothing bound or no credential
 * looks healthy while no partner can possibly connect.
 *
 * Deliberately narrow, because the outbound card's own note is right that two
 * places reporting one state is what this control plane avoids. So this states
 * the VERDICT and the blocker — neither of which is derivable at a glance from
 * anything else — and leaves the counts to the Overview tab and the credential
 * itself to the card above. The one fact it repeats is never-connected, which
 * Overview renders as a bare "—" with no explanation and which is the single
 * most useful thing to know when inbound is not working.
 *
 * Like the outbound card, it must not overstate: holding a credential and
 * exposing an agent is everything this platform can check. Whether the partner
 * has the matching SECRET is proven only by its first token exchange, and
 * saying so is the honest counterpart to the outbound card's unsigned-card
 * warning.
 */
export function A2AInboundReachabilityCard({
  integration,
}: {
  integration: Integration;
}) {
  const { t_i18n } = useFormatter();
  const direction = useA2ADirection(integration);
  const { bound: boundAgents, states } = useBoundAgentCards(integration.id);
  const { data: activity } = useA2APartnerActivity(integration.id);

  // Outbound has its own card.
  if (direction !== "inbound") return null;

  // "Ready" needs every bound agent this viewer can read to have a live card
  // (#11786): a binding that published nothing is unreachable however
  // healthy the credential is.
  const missing = missingCardCount(states);
  // Only the OAuth mechanism mints a secret. Federated partners present their
  // own token, verified against pins; saved with none, the registration is
  // left `disconnected` by the API.
  const needsMintedCredential = integration.provider === OAUTH_A2A_PROVIDER;
  const hasCredential = needsMintedCredential
    ? typeof integration.extra_config?.client_id === "string"
    : integration.status !== "disconnected";
  const everConnected = Boolean(activity?.last_activity_at);

  // ``agents`` holds what THIS viewer can see, so an empty ``boundAgents`` is
  // "none that you can see" — not "none". A company-managed registration bound
  // to someone's private agent is serving correctly, and a second operator was
  // being told it was broken. Evidence of traffic settles it; otherwise the
  // wording stays honest about what it knows.
  const blocker = !hasCredential
    ? needsMintedCredential
      ? t_i18n("No credential has been generated, so the partner has nothing to authenticate with.")
      : t_i18n("No federation pins are configured, so no partner token can verify. Add them in the configuration above.")
    : boundAgents.length === 0 && !everConnected
      ? t_i18n("No agent you can see binds this integration. If none does, bind it in an agent's Tools tab: the partner has nothing to reach until then.")
      : missing > 0
        ? t_i18n("{count, plural, one {# bound agent has} other {# bound agents have}} no published card, so the partner cannot reach {count, plural, one {it} other {them}}. Agent card URLs below says why.", { count: missing })
        : null;

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="content-base flex items-center gap-2">
          {blocker ? (
            <ShieldAlert className="h-4 w-4 text-feedback-warning-primary" />
          ) : (
            <ShieldCheck className="h-4 w-4 text-accent" />
          )}
          {t_i18n("Partner reachability")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {blocker ? (
          <p className="content-compact text-feedback-warning-primary">{blocker}</p>
        ) : (
          <p className="content-compact">
            {everConnected
              ? t_i18n("Ready, and this partner has connected.")
              : t_i18n("Ready. This partner has not connected yet.")}
          </p>
        )}
        <p className="content-caption text-default-secondary">
          {t_i18n(
            "Holding a credential and exposing an agent is everything this platform can check from here. Whether the partner holds the matching secret is proven only by its first token exchange.",
          )}
        </p>
      </CardContent>
    </Card>
  );
}
