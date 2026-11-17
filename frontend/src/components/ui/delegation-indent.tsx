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
 * One guide column per delegation level below the first, so a run a
 * delegated agent delegated sits under that agent's run instead of beside it.
 * Pair with `orderRunTree()` from `@/lib/run-tree`, which gives the depth.
 */
export function DelegationIndent({ depth }: { depth: number }) {
  if (depth <= 1) return null;
  return (
    <>
      {Array.from({ length: depth - 1 }, (_, level) => (
        <div key={level} aria-hidden="true" className="w-5 self-stretch flex justify-center shrink-0">
          <div className="w-px bg-accent/15" />
        </div>
      ))}
    </>
  );
}
