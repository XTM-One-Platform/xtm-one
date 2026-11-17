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

import { LABEL_TIP_STYLE } from "./common";

/** The label-only tooltip (`tooltipContent: "label"`), at a point of its positioned parent. */
export function LabelTip({ label, x, y }: { label: string; x: number; y: number }) {
  return (
    <div role="tooltip" className="pointer-events-none absolute z-10 whitespace-nowrap" style={{ ...LABEL_TIP_STYLE, left: x + 12, top: y + 12 }}>
      {label}
    </div>
  );
}
