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
 * The second line of a fleet member row: the role label, then the
 * health parts, joined by " · ". The visible line and its `title` (the whole line, read
 * when the caption truncates in the narrow panel) are both rendered from the same
 * `parts`, so they cannot drift apart. The role label, when given, leads in the primary
 * text colour.
 */
export function FleetMemberCaption({
  roleLabel,
  parts,
}: {
  roleLabel?: string | null;
  parts: string[];
}) {
  const line = [roleLabel, ...parts].filter((part): part is string => typeof part === "string" && part !== "");
  if (line.length === 0) return null;
  const leadIsRole = Boolean(roleLabel);
  return (
    <span className="block truncate content-caption text-default-secondary" title={line.join(" · ")}>
      {line.map((part, index) => (
        <span
          key={index}
          data-part={index === 0 && leadIsRole ? "role" : "health"}
          className={index === 0 && leadIsRole ? "text-default-primary" : undefined}
        >
          {index > 0 && " · "}
          {part}
        </span>
      ))}
    </span>
  );
}
