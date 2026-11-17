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

/** A proposal's evidence (#8261): a link where the viewer may open it, "hidden" otherwise, never its id. */
import { Link } from "react-router-dom";
import { useImprovementEvidence } from "@/hooks/api/useConciergeApi";
import { useFormatter } from "@/i18n";
import { EVIDENCE_KIND_LABELS, labelOf } from "@/lib/concierge";

export function ImprovementEvidence({ proposalId }: { proposalId: string }) {
  const { t_i18n } = useFormatter();
  const { data: links = [], isLoading } = useImprovementEvidence(proposalId);
  if (isLoading || !links.length) return null;
  const hidden = links.filter((link) => link.hidden).length;
  return (
    <div className="space-y-1">
      <p className="content-caption text-default-secondary">{t_i18n("Evidence")}</p>
      <div className="flex flex-wrap gap-2">
        {links
          .filter((link) => link.href)
          .map((link, index) => (
            <Link key={`${link.id}-${index}`} to={link.href as string} className="content-compact text-accent hover:underline">
              {t_i18n(labelOf(EVIDENCE_KIND_LABELS, link.kind))} {index + 1}
            </Link>
          ))}
      </div>
      {hidden > 0 && (
        <p className="content-caption text-default-secondary">
          {t_i18n("{count, plural, one {# more item of evidence you may not open.} other {# more items of evidence you may not open.}}", { count: hidden })}
        </p>
      )}
    </div>
  );
}
