// SPDX-License-Identifier: Apache-2.0

import { ConfigurationCard } from "./ConfigurationCard";
import { EmailSignatureCard } from "./EmailSignatureCard";
import { ToolsCard } from "./ToolsCard";
import { EventTriggersCard } from "./EventTriggersCard";
import { WebhooksCard } from "./WebhooksCard";
import { webhooksCardShowsBlock } from "./webhooks-card-blocks";
import { WhatsAppPairingCard } from "./WhatsAppPairingCard";
import { isWhatsAppGateway } from "./whatsapp-gateway";
import { A2ACredentialCard, A2AExposedCard, A2AInboundReachabilityCard, A2AToolsCard, A2AUsageCard } from "./A2AProtocolCards";
import { A2AVerificationCard } from "./A2AVerificationCard";
import { A2ACardUrlsCard } from "./A2ACardUrlsCard";
import { A2ADelegationsCard } from "./A2ADelegationsCard";
import { isA2AProvider, OAUTH_A2A_PROVIDER } from "@/hooks/api/useA2AApi";
import { A2A_CLIENT_PROVIDERS, useA2ADirection } from "@/hooks/api/useA2AClientApi";
import type { Integration, ProviderCatalogItem } from "@/types";

export function ConfigTab({ integration, catalogEntry, canManage, isRegistrationOnly, baseUrl }: { integration: Integration; catalogEntry?: ProviderCatalogItem; canManage: boolean; isRegistrationOnly: boolean; baseUrl: string }) {
  // The federation provider's "tools" are INBOUND protocol operations, so it
  // gets its own pair of cards instead of the generic tool list: nothing here
  // is callable from our side, which makes the generic card's "available to
  // any agent" promise and its per-tool Test button both wrong.
  const isA2A = isA2AProvider(integration.provider);
  // A different question from `isA2A` — "can this mechanism face outward" —
  // even though every shipped mechanism answers both the same way today.
  const isA2AOutboundCapable = (A2A_CLIENT_PROVIDERS as readonly string[]).includes(
    integration.provider,
  );

  // Left `undefined` until known, so the configuration card neither hides a
  // row that belongs to it nor briefly withholds the fields to edit.
  const a2aDirection = useA2ADirection(integration);

  // Whether the credential card below owns the client id. It gates itself on
  // direction — like every other card here — so this is NOT that gate; it is
  // only how the configuration card learns that another section is already
  // displaying the same value.
  const credentialCardOwnsClientId =
    integration.provider === OAUTH_A2A_PROVIDER && a2aDirection === "inbound";

  // Which stored values another section already displays, so the generic
  // `extra_config` list below does not repeat them.
  //
  // The card URLs go for the same reason the minted client id does: the
  // verification card lists one panel per agent, each headed by what that
  // agent declares and carrying its own verdict. Repeating the raw URLs here
  // as one undifferentiated blob says less and disagrees the moment discovery
  // adds an agent — the operator's typed list and the targets actually
  // reached are not the same thing, and only one of them is worth showing.
  // Still EDITABLE: the edit form renders the field from the catalog, which
  // is how a partner without discovery gets its list maintained.
  // An OpenAPI integration's client certificate has its own card in the API
  // Spec tab.
  const hiddenExtraKeys = [
    ...(credentialCardOwnsClientId ? ["client_id"] : []),
    ...(a2aDirection === "outbound" ? ["card_urls", "card_url"] : []),
    ...(catalogEntry?.spec_import ? ["client_certificate"] : []),
  ];

  const showsWebhooks = webhooksCardShowsBlock(integration, catalogEntry, canManage);

  return (
    <div className="space-y-6">
      <ConfigurationCard
        integration={integration}
        catalogEntry={catalogEntry}
        canManage={canManage}
        isRegistrationOnly={isRegistrationOnly}
        a2aDirection={a2aDirection}
        hiddenExtraKeys={hiddenExtraKeys}
      />

      {/* A WhatsApp number paired through a linked-device gateway: its pairing
          state, the QR to scan and the gateway webhook URL. */}
      {isWhatsAppGateway(integration) && (
        <WhatsAppPairingCard integration={integration} baseUrl={baseUrl} />
      )}

      {/* Renders itself away for an integration whose tools send no email. */}
      <EmailSignatureCard integration={integration} canManage={canManage} />

      {/* The two halves of "can this partner relationship actually work",
          one per direction. Each renders itself away for the other: the
          outbound pair reads the verification state an inbound registration
          has none of, and the inbound card answers the mirror question — can
          the partner reach US, and what is stopping it. */}
      {isA2AOutboundCapable && a2aDirection === "outbound" && (
        <>
          <A2AVerificationCard integration={integration} canManage={canManage} />
          {/* Manage authority, like the credential card below: the history
              names the agents that delegated, and its endpoint refuses a
              viewer — rendering it for one would 403 into the error
              boundary. The verification card above needs sight only. */}
          {canManage && <A2ADelegationsCard integration={integration} />}
        </>
      )}
      {isA2A && <A2AInboundReachabilityCard integration={integration} />}

      {/* Directly above the credential: the URLs are what a partner fetches
          and the credential is what it presents, so the two things an
          operator hands over sit together. Every inbound mechanism publishes
          cards, not just the one that mints a secret. */}
      {isA2A && a2aDirection === "inbound" && (
        <A2ACardUrlsCard integration={integration} />
      )}

      {/* Inbound OAuth A2A IS a credential: the client id re-displays, the
          secret is shown once at mint. Renders itself away for an outbound
          registration, which presents credentials the partner issued and
          edits them in the configuration card above. Foundry A2A carries
          pinned facts instead, edited there too. */}
      {integration.provider === OAUTH_A2A_PROVIDER && (
        <A2ACredentialCard integration={integration} canManage={canManage} />
      )}

      {/* Two questions every integration answers, asked of both directions:
          what a partner may invoke here, and what this binding gives your
          agents. A2A answers them differently each way — inbound serves the
          task surface and confers one tool, outbound serves one callback
          route and confers one tool per verified partner agent — which is
          why it renders its own pair rather than the generic tool card. The
          usage narrative stays inbound: it walks what a partner does to us. */}
      {catalogEntry && (isA2A ? (
        <>
          <A2AExposedCard integration={integration} catalogEntry={catalogEntry} />
          <A2AToolsCard integration={integration} catalogEntry={catalogEntry} />
          {a2aDirection === "inbound" && <A2AUsageCard />}
        </>
      ) : (
        <ToolsCard catalogEntry={catalogEntry} integration={integration} />
      ))}

      {/* Event Triggers and Webhooks share a row; when the Webhooks card
          renders nothing (every event polled, nothing else to show),
          Event Triggers takes the whole row instead of leaving the right
          column empty. */}
      <div
        className={showsWebhooks ? "grid gap-6 lg:grid-cols-2" : "grid gap-6"}
        data-testid="config-events-row"
        data-columns={showsWebhooks ? "2" : "1"}
      >
        {catalogEntry && <EventTriggersCard catalogEntry={catalogEntry} />}
        <WebhooksCard integration={integration} catalogEntry={catalogEntry} baseUrl={baseUrl} canManage={canManage} />
      </div>
    </div>
  );
}
