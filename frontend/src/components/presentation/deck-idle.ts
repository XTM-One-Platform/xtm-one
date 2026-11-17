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
 * Stillness before the presenting toolbar - and the pointer with it - leave
 * the screen.
 *
 * A plain module (no React, no DOM) so the e2e spec that times the idle hide
 * (`tests_e2e/presentation-fullscreen.spec.ts`) reads the number the stage
 * arms its timer with, the way `spread-math.ts` is read by the fill specs -
 * never a copy of the value that drifts the day the stage changes it.
 */
export const IDLE_HIDE_MS = 2500;
