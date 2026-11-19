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
 * The Transport and Authentication pickers of an MCP server, shared by the
 * create form (NewServerForm) and the Configuration edit view (ConfigTab) so
 * both pages read the same translated labels at the same 36 px height
 * (form standard sweep #9375, UX round 1 of #9991, B2). A page never renders
 * its own copy of these buttons (connection-pickers.test.ts).
 */
import type { ReactNode } from "react";
import { Globe, KeyRound, Lock, Radio, Terminal, User, Users } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { useFormatter } from "@/i18n";
import type { MCPCredentialMode } from "@/types";

export type MCPTransport = "stdio" | "sse" | "streamable_http";
export type MCPAuthMode = "headers" | "oauth";

export function TransportPicker({ value, onChange }: { value: MCPTransport; onChange: (value: MCPTransport) => void }) {
  const { t_i18n } = useFormatter();
  const options: { value: MCPTransport; label: string; Icon: typeof Terminal }[] = [
    { value: "stdio", label: t_i18n("Subprocess (stdio)"), Icon: Terminal },
    { value: "streamable_http", label: "Streamable HTTP", Icon: Globe },
    { value: "sse", label: t_i18n("SSE (legacy)"), Icon: Radio },
  ];
  return (
    <div className="space-y-1.5 sm:col-span-2">
      <Label className="block text-default-primary">{t_i18n("Transport")}</Label>
      <div className="flex flex-wrap gap-2" role="group" aria-label={t_i18n("Transport")}>
        {options.map(({ value: option, label, Icon }) => (
          <Button
            key={option}
            type="button"
            variant={value === option ? "default" : "outline"}
            aria-pressed={value === option}
            onClick={() => onChange(option)}
            className="flex-1 gap-1.5"
          >
            <Icon className="h-3.5 w-3.5" /> {label}
          </Button>
        ))}
      </div>
    </div>
  );
}

export function AuthModePicker({
  value,
  onChange,
  children,
}: {
  value: MCPAuthMode;
  onChange: (value: MCPAuthMode) => void;
  /** A note under the buttons (the edit view's token warning). */
  children?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="space-y-1.5">
      <Label className="block text-default-primary">{t_i18n("Authentication")}</Label>
      <div className="flex gap-2" role="group" aria-label={t_i18n("Authentication")}>
        <Button type="button" variant={value === "headers" ? "default" : "outline"} aria-pressed={value === "headers"} onClick={() => onChange("headers")} className="flex-1 gap-1.5">
          <KeyRound className="h-3.5 w-3.5" /> {t_i18n("Secret Headers")}
        </Button>
        <Button type="button" variant={value === "oauth" ? "default" : "outline"} aria-pressed={value === "oauth"} onClick={() => onChange("oauth")} className="flex-1 gap-1.5">
          <Lock className="h-3.5 w-3.5" /> OAuth 2.0
        </Button>
      </div>
      {children}
    </div>
  );
}

/**
 * Whose account an OAuth server's calls carry (#443): one connection shared by
 * everyone who can use the server, or each person's own account.
 */
export function CredentialModePicker({
  value,
  onChange,
  children,
}: {
  value: MCPCredentialMode;
  onChange: (value: MCPCredentialMode) => void;
  /** A note under the buttons (what the change discards). */
  children?: ReactNode;
}) {
  const { t_i18n } = useFormatter();
  const helper =
    value === "per_user"
      ? t_i18n("Each person connects their own account once and acts as themselves.")
      : t_i18n("One account, authorized once, is used for every person who can use this server.");
  return (
    <div className="space-y-1.5">
      <Label className="block text-default-primary">{t_i18n("Connected account")}</Label>
      <div className="flex gap-2" role="group" aria-label={t_i18n("Connected account")}>
        <Button type="button" variant={value === "shared" ? "default" : "outline"} aria-pressed={value === "shared"} onClick={() => onChange("shared")} className="flex-1 gap-1.5">
          <Users className="h-3.5 w-3.5" /> {t_i18n("One shared account")}
        </Button>
        <Button type="button" variant={value === "per_user" ? "default" : "outline"} aria-pressed={value === "per_user"} onClick={() => onChange("per_user")} className="flex-1 gap-1.5">
          <User className="h-3.5 w-3.5" /> {t_i18n("Each person's own account")}
        </Button>
      </div>
      <p className="content-compact text-default-secondary">{helper}</p>
      {children}
    </div>
  );
}
