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
 * The icon catalog as one icon waits for it: `DeckIcon` and the
 * architecture block's `SvgIcon` share it.
 *
 * The catalog is one lazy chunk shared by every icon (`deck-icons.ts`); until
 * it lands an icon draws a placeholder of its final size, which holds the
 * deck's ready signal (`waiting`). An icon whose catalog failed to load, or
 * has not landed within `FIGURE_WAIT_MS` of visible time, keeps its
 * placeholder and lets go of the signal: a missing icon never holds the print.
 * The next icon mounted retries the load.
 */
import { useEffect, useState, useSyncExternalStore, type RefObject } from "react";

import {
  type DeckIconCatalog,
  getDeckIconCatalog,
  loadDeckIconCatalog,
  subscribeDeckIconCatalog,
} from "./deck-icons";
import { useGiveUp } from "./figure-wait";

export function useDeckIconCatalog(
  placeholder: RefObject<Element | null>,
  wanted: boolean,
): { catalog: DeckIconCatalog | null; waiting: boolean } {
  const catalog = useSyncExternalStore(subscribeDeckIconCatalog, getDeckIconCatalog, getDeckIconCatalog);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (catalog || !wanted) return;
    let live = true;
    loadDeckIconCatalog().catch(() => {
      if (live) setFailed(true);
    });
    return () => {
      live = false;
    };
  }, [catalog, wanted]);
  const gaveUp = useGiveUp(placeholder, catalog || failed || !wanted ? null : "catalog");
  return { catalog, waiting: !catalog && !failed && !gaveUp };
}
