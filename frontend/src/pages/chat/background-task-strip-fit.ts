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

/** Room one chip takes in the strip (its 180 px cap shrinks to fit), gap included. */
const CHIP_SLOT_PX = 150;
/** Room kept for the "+N more" counter once some chips are folded into it. */
const COUNTER_RESERVE_PX = 72;
/** Chips shown before the strip has been measured (jsdom, first paint). */
const UNMEASURED_CHIPS = 3;

/**
 * How many of `total` chips fit a strip `width` px wide: all of them when they
 * fit, else as many as leave room for the "+N more" counter, at least one.
 */
export function fitChipCount(width: number, total: number): number {
  if (total <= 0) return 0;
  if (width <= 0) return Math.min(total, UNMEASURED_CHIPS);
  if (total * CHIP_SLOT_PX <= width) return total;
  return Math.max(1, Math.min(total - 1, Math.floor((width - COUNTER_RESERVE_PX) / CHIP_SLOT_PX)));
}
