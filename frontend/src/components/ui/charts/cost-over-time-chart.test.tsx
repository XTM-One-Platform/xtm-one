// The cost over time card reads its loading and empty states in the viewer's
// language, like its cost-by-model sibling.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { IntlProvider } from "react-intl";

import fr from "@/i18n/lang/fr.json";
import CostOverTimeChart from "./cost-over-time-chart";

describe("CostOverTimeChart", () => {
  it("reads the empty state in French under the fr catalog", () => {
    render(
      <IntlProvider locale="fr-fr" messages={fr} onError={() => {}}>
        <CostOverTimeChart series={[]} />
      </IntlProvider>,
    );
    expect(screen.getByText("Aucune donnée de coût dans le temps pour ces filtres.")).toBeTruthy();
  });

  it("reads the loading state in French under the fr catalog", () => {
    render(
      <IntlProvider locale="fr-fr" messages={fr} onError={() => {}}>
        <CostOverTimeChart isLoading />
      </IntlProvider>,
    );
    expect(screen.getByText("Chargement du coût dans le temps...")).toBeTruthy();
  });
});
