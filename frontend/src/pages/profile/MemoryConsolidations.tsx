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

/** Profile > Memory: the Memory Steward's changes to the user's own lanes (#8258). */

import { useState } from "react";
import { MemoryConsolidationsCard } from "@/components/memory/MemoryConsolidationsCard";
import { useMyMemoryConsolidations, useUndoMyMemoryConsolidation } from "@/hooks/api/useMemoryConsolidationsApi";

export function MyMemoryConsolidations() {
  const { data } = useMyMemoryConsolidations();
  const undo = useUndoMyMemoryConsolidation();
  const [undoingId, setUndoingId] = useState<string | null>(null);

  // Nothing to show until the Steward has changed something.
  if (!data || data.consolidations.length === 0) return null;

  return (
    <MemoryConsolidationsCard
      items={data.consolidations}
      retentionDays={data.retention_days}
      showAgent
      undoingId={undoingId}
      onUndo={(id, callbacks) => {
        setUndoingId(id);
        undo.mutate(id, { ...callbacks, onSettled: () => setUndoingId(null) });
      }}
    />
  );
}
