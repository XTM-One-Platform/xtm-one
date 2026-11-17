// The version diff dialog's own words go through t_i18n: "Unified diff (raw)",
// "No textual diff." and "No diff data available." were raw English strings, shown in English
// to a French, Spanish or Japanese user.
import { render, screen } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { describe, expect, it } from "vitest";

import fr from "@/i18n/lang/fr.json";
import type { VersionDiffResponse } from "@/types";

import { VersionDiffDialog } from "./version-diff-dialog";

const diff = {
  from_version: 1,
  to_version: 2,
  added: 0,
  removed: 0,
  lines: [],
  unified_diff: "",
} as unknown as VersionDiffResponse;

function inFrench(ui: React.ReactElement) {
  return render(
    <IntlProvider locale="fr" messages={fr as Record<string, string>} onError={() => {}}>
      {ui}
    </IntlProvider>,
  );
}

describe("VersionDiffDialog", () => {
  it("names the raw diff and its empty state in the user's language", () => {
    inFrench(<VersionDiffDialog open onOpenChange={() => {}} diff={diff} title="v1 -> v2" />);
    expect(screen.getByText(fr["Unified diff (raw)"])).toBeTruthy();
    expect(screen.getByText(fr["No textual diff."])).toBeTruthy();
    expect(screen.queryByText("Unified diff (raw)")).toBeNull();
  });

  it("says there is no diff data in the user's language", () => {
    inFrench(<VersionDiffDialog open onOpenChange={() => {}} diff={null} title="v1 -> v2" />);
    expect(screen.getByText(fr["No diff data available."])).toBeTruthy();
  });
});
