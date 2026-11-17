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
 * The validation views: outcomes by label drawn as the security posture
 * widgets they reproduce (`dev-docs/dashboard-rendering-reference.md`,
 * OpenAEV sections 9-14), one renderer per kind:
 *
 * - `outcomematrix` (`OutcomeMatrix`): labels in columns by group, one chain at a time;
 * - `pathgraph` (`PathGraph`): outcomes by group, linked on hover, with the run's dates;
 * - `exposureconsole` (`ExposureConsole`): an exposure orb, an attack flow and calls to action;
 * - `scoregauge` (`ScoreGauge`): an exposure score on a 270° gauge;
 * - `ringgauge` (`RingGauge`): a resilience on a segmented ring;
 * - `domaincards` (`DomainCards`): a card per domain with its parts.
 *
 * A view whose data has no count yet shows its sample when its options say
 * how (`sampleDisplay`: greyed or desaturated), else that there is no data.
 */
import { useFormatter } from "@/i18n";
import type { WidgetOutcomesData } from "@/types";

import { SAMPLE_GATES, SAMPLE_OUTCOMES, SAMPLE_STATUSES, hasCounts, type OutcomeView } from "../viz/outcomes";
import DomainCards from "./DomainCards";
import ExposureConsole from "./ExposureConsole";
import { emptyNote } from "./look";
import { OutcomeEmpty, SampleFrame } from "./outcome-common";
import type { SampleDisplay } from "./outcome-hooks";
import OutcomeMatrix from "./OutcomeMatrix";
import PathGraph from "./PathGraph";
import RingGauge from "./RingGauge";
import ScoreGauge from "./ScoreGauge";

const sampleDisplay = (options: Record<string, unknown>): SampleDisplay =>
  options.sampleDisplay === "full" || options.sampleDisplay === "subtle" ? options.sampleDisplay : "off";

export default function OutcomeViews({
  data,
  view,
  height,
  options,
}: {
  data: WidgetOutcomesData;
  view: OutcomeView;
  height: number;
  options: Record<string, unknown>;
}) {
  const { t_i18n } = useFormatter();
  const sample = sampleDisplay(options);
  const nothing = <OutcomeEmpty message={t_i18n(emptyNote(options).message)} />;
  const counted = hasCounts(data);
  const framed = (drawing: React.ReactNode) => (
    <div className="h-full" style={{ height }}>
      {drawing}
    </div>
  );
  switch (view) {
    case "outcomematrix":
      return data.items.length === 0 && !data.reference?.length ? nothing : <OutcomeMatrix data={data} height={height} />;
    case "pathgraph":
      return data.items.length === 0 && !data.needsScope && !data.timeline ? nothing : <PathGraph data={data} height={height} />;
    case "domaincards":
      return data.items.length === 0 && !data.reference?.length ? nothing : framed(<DomainCards data={data} />);
    case "exposureconsole":
      if (counted) return framed(<ExposureConsole data={data} />);
      return sample === "off" ? nothing : framed(<SampleFrame display={sample}><ExposureConsole data={{ ...SAMPLE_GATES, orbit: data.orbit, actions: data.actions }} /></SampleFrame>);
    case "scoregauge":
      if (counted) return framed(<ScoreGauge data={data} />);
      return sample === "off" ? nothing : framed(<SampleFrame display={sample}><ScoreGauge data={SAMPLE_OUTCOMES} /></SampleFrame>);
    case "ringgauge": {
      const first = data.items[0];
      if (first && first.success + first.failed + first.pending > 0) return framed(<RingGauge counts={first} icon={first.icon} />);
      return sample === "off" ? nothing : framed(<SampleFrame display={sample}><RingGauge counts={{ ...SAMPLE_STATUSES, label: first?.label ?? "" }} icon={first?.icon} /></SampleFrame>);
    }
  }
}
