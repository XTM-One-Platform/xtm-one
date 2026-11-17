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

/** Who external work runs as, for an agent exposed over inbound A2A.
 *
 * Inherited (the agent's owner) rather than chosen, so it is stated plainly
 * instead of left implicit — a partner submitting work sees exactly what that
 * user sees.
 *
 * The agent's card URL used to sit here too. It lives on the integration's
 * Config tab now (`A2ACardUrlsCard`), one row per bound agent, so an operator
 * handing a partner their details reads it all off one page. This is the
 * per-agent fact that stays with the agent.
 */

import { UserCog } from "lucide-react";
import { useUsers } from "@/hooks/api/useAuthApi";
import { useFormatter } from "@/i18n";
import type { A2AExposureStatus } from "@/hooks/api/useA2AApi";
import type { Agent } from "@/types";

export function A2ARunAs({ agent, exposure }: { agent: Agent; exposure: A2AExposureStatus }) {
  const { t_i18n } = useFormatter();
  // /users is admin-only, so only fetch where the run-as line needs a name.
  const { data: users } = useUsers(agent.is_company_managed);

  const runner = users?.find((u) => u.id === exposure.run_as_user_id);
  const runnerLabel = runner
    ? (runner.full_name?.trim() ? `${runner.full_name} (${runner.email})` : runner.email)
    : null;

  return (
    <div className="flex items-center gap-1.5 rounded-sm bg-elevation-hover px-3 py-2.5 content-caption text-default-secondary">
      <UserCog className="h-3.5 w-3.5 shrink-0 text-accent" />
      {runnerLabel
        ? t_i18n("External work runs as {user}: partners see exactly what that user sees.", { user: runnerLabel })
        : t_i18n("External work runs as this agent's owner: partners see exactly what that user sees.")}
    </div>
  );
}
