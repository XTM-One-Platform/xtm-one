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

import { createContext, useContext } from "react";
import type { AppLockStatus } from "@/types";

/** What the companion's PIN lock (`AppLockGate`) offers the screens behind it. */
export interface AppLockControls {
  status: AppLockStatus;
  /** Lock the companion now. */
  lockNow: () => void;
  /** Open the screen that chooses a PIN (the person has none yet). */
  choosePin: () => void;
}

export const AppLockContext = createContext<AppLockControls | null>(null);

/** The companion's lock, behind the gate on a phone; `null` on a computer. */
export function useAppLock(): AppLockControls | null {
  return useContext(AppLockContext);
}
