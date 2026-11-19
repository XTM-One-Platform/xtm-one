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
