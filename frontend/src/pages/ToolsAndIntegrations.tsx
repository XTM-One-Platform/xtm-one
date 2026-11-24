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
 * Tools & Integrations hub (`/tools-and-integrations`) — the destination behind
 * the sidebar group of the same name: the capabilities agents act *through*
 * (MCP servers, custom tools), the services they act *on* (integrations), and
 * the variables those configurations interpolate.
 *
 * Variables are gated on `custom_variables`, exactly as in the sidebar; the
 * query is disabled when the feature is off so the page makes no call the
 * backend would refuse. The cards are the group's visible children
 * (`hubCards`, #9301 N2c): the menu decides which show, in its order.
 */
import { ResourceHub } from "@/components/ui/resource-hub";
import { hubCards, useNavGroup } from "@/components/layout/use-nav-group";
import { useBrowserProfiles } from "@/hooks/api/useBrowserProfilesApi";
import { useCustomTools } from "@/hooks/api/useCustomToolApi";
import { useMCPServers } from "@/hooks/api/useMCPServerApi";
import { useIntegrations } from "@/hooks/api/useIntegrationApi";
import { useRelayGroups, useRelaysEnabled } from "@/hooks/api/useRelayApi";
import { useVariables } from "@/hooks/api/useVariableApi";
import { useCustomVariablesEnabled } from "@/hooks/usePlatformConfig";

export default function ToolsAndIntegrations() {
  const customVariablesEnabled = useCustomVariablesEnabled();
  const relaysEnabled = useRelaysEnabled() === true;
  const tools = useCustomTools();
  const servers = useMCPServers();
  const integrations = useIntegrations();
  const browserProfiles = useBrowserProfiles();
  const variables = useVariables({ enabled: customVariablesEnabled });
  const relayGroups = useRelayGroups({ enabled: relaysEnabled });

  /* Same order as the sidebar group: MCP servers first — the richest capability
     an agent acts through, and the page that also carries the MCP endpoints this
     platform exposes. */
  const resources = hubCards(useNavGroup("tools-and-integrations"), {
    "/mcp-servers": {
      createTo: "/mcp-servers/new",
      gatedCreate: true,
      description: "External tool servers speaking the Model Context Protocol, discovered and bound per agent, and the MCP endpoints this platform exposes.",
      items: servers.data,
      isLoading: servers.isLoading,
      detailTo: (item) => `/mcp-servers/${item.id}`,
    },
    "/tools": {
      createTo: "/tools/new",
      gatedCreate: true,
      requiresAi: false,  // a Custom tool is a sandboxed script (T077)
      description: "Your own functions, run in a sandbox: the fastest way to give agents a new capability.",
      items: tools.data,
      isLoading: tools.isLoading,
      detailTo: (item) => `/tools/${item.id}`,
    },
    "/integrations": {
      createTo: "/integrations/new",
      description: "Connections to the services agents read from and write to, with credentials encrypted at rest.",
      items: integrations.data,
      isLoading: integrations.isLoading,
      detailTo: (item) => `/integrations/${item.id}`,
    },
    "/connections": {
      /* Created from a dialog on the list page, like variables. */
      createTo: "/connections?new=1",
      requiresAi: false,  // a saved sign-in is managed with AI off
      description: "Saved sign-ins to web applications: the agents you give one browse signed in, without seeing the credentials.",
      items: browserProfiles.data?.map((p) => ({ id: p.id, name: p.name, updated_at: p.updated_at ?? p.created_at ?? "" })),
      isLoading: browserProfiles.isLoading,
    },
    "/connections/variables": {
      /* Variables are created from a dialog on the list page rather than a
         route of their own, so the card deep-links that dialog open. */
      createTo: "/connections/variables?new=1",
      gatedCreate: true,
      requiresAi: false,  // custom variables are not AI
      description: "Named values reused across prompts, tools and integrations instead of hard-coded strings.",
      items: variables.data,
      isLoading: variables.isLoading,
    },
    "/connections/relays": {
      requiresAi: false,  // relays carry integration traffic, not AI
      description: "Pull-based relays that reach the services inside your networks, with no inbound flow.",
      items: relayGroups.data?.map((g) => ({ id: g.id, name: g.name, updated_at: g.updated_at ?? g.created_at ?? "" })),
      isLoading: relayGroups.isLoading,
      detailTo: (item) => `/connections/relays/${item.id}`,
    },
  });

  return (
    <ResourceHub
      title="Tools & Integrations"
      description="Capabilities and connections your agents act through."
      resources={resources}
    />
  );
}
