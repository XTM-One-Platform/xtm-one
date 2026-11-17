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

import { RunActivityList } from "@/components/ui/run-activity-list";

// ---------------------------------------------------------------------------
// Activity Tab: the assignment's runs on the shared runs table (#9378 slice 4a).
// Every run for an admin, own and company-managed runs for anyone else: the
// list, the count and the status counts read one filtered query on the server.
// ---------------------------------------------------------------------------
export function ActivityTab({ assignmentId }: { assignmentId: string }) {
  return <RunActivityList storageKey="assignment" entityLabel="assignment" assignmentId={assignmentId} reviewFilter />;
}
