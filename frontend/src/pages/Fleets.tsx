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
 * Fleets (`/fleets`) - the fleets the user can see, a page of its own (#9301
 * N2c). It was the Fleets tab of Agentic Flow (`/flows/fleets`), so the
 * "Fleets" and "Agentic Flow" rows of the Agents group opened the same page;
 * one fleet opens on `/fleets/:fleetId/:tab?` (About by default). The former
 * addresses are not redirected (#797).
 *
 * The header is the shared `ListPageHeader` (#9301 N3, as the other list
 * pages of the menu): the h1 "Fleets" with the one-line definition of a fleet
 * in its "i", the actions at the end of the same row. The list body is
 * `FleetsTab`, unchanged: its creation actions ("Create with a prompt", "New
 * fleet") render into the header's action slot through the same context
 * Agentic Flow offers, so the toolbar keeps its row for the filters.
 */

import { useState } from "react";
import { useFormatter } from "@/i18n";
import { ListPageHeader } from "@/components/ui/list-page-header";
import FleetsTab from "@/pages/flows/FleetsTab";
import { FlowsHeaderSlotContext } from "@/pages/flows/header-slot";

export default function Fleets() {
  const { t_i18n } = useFormatter();
  // A state, not a ref: the list renders into the slot once it is mounted.
  const [headerSlot, setHeaderSlot] = useState<HTMLElement | null>(null);

  return (
    <div className="space-y-6">
      <ListPageHeader
        title={t_i18n("Fleets")}
        infoLabel={t_i18n("About fleets")}
        info={t_i18n("A fleet is a team of agents. It works in one workspace, where everything its agents produce is kept.")}
        actions={<div ref={setHeaderSlot} className="flex items-center" />}
      />
      <FlowsHeaderSlotContext.Provider value={headerSlot}>
        <FleetsTab />
      </FlowsHeaderSlotContext.Provider>
    </div>
  );
}
