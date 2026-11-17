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
 * The links of a proactive send to what produced it: the agent, the
 * conversation, the run and the assignment its origin names, each one opening
 * its own page. Nothing when the origin names none.
 */
import { Link } from "react-router-dom";
import { useFormatter } from "@/i18n";
import type { AgentAccountSendOrigin } from "@/types";
import { SEND_ORIGIN_LINK_LABELS, sendOriginLinks } from "./agent-accounts";

export function SendOriginLinks({ origin, className }: { origin: AgentAccountSendOrigin | null | undefined; className?: string }) {
  const { t_i18n } = useFormatter();
  const links = sendOriginLinks(origin);
  if (links.length === 0) return null;
  return (
    <span className={className ?? "inline-flex flex-wrap gap-x-3 gap-y-1"}>
      {links.map(({ key, href }) => {
        const label = t_i18n(SEND_ORIGIN_LINK_LABELS[key]);
        return (
          <Link
            key={key}
            to={href}
            aria-label={t_i18n("Open the {item} that produced this send", { item: label })}
            className="content-compact text-accent underline-offset-2 hover:underline"
          >
            {label}
          </Link>
        );
      })}
    </span>
  );
}
