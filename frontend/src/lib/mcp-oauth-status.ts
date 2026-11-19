// SPDX-License-Identifier: Apache-2.0

import type { MCPServer } from "@/types";

type OAuthStatus = MCPServer["oauth_status"];

/**
 * The OAuth connection a reader cares about (#443): on a `per_user` server the
 * reader's own account (`my_oauth_status`), never the server row's token,
 * which is not used there; on a `shared` server the server's one connection.
 */
export function readerOAuthStatus(srv: Pick<MCPServer, "oauth_status" | "credential_mode" | "my_oauth_status">): OAuthStatus {
  if (srv.credential_mode === "per_user") return srv.my_oauth_status ?? "disconnected";
  return srv.oauth_status;
}
