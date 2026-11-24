// SPDX-License-Identifier: Apache-2.0
// CTEM-25-UI (#1098): an Aikido signal reads like any other in the loop trace:
// its source named "Aikido Security", the finding type in words, the
// repository or image and the package in their own rows, never as raw
// evidence lines.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { CtemSignal } from "@/hooks/api/useCtemSignalsApi";
import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import { SignalTraceBody, aikidoFacts, signalProviderName } from "./SignalLoopTrace";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn() },
}));

const get = vi.mocked(api.get);
const ID = "00000000-0000-0000-0000-000000000250";

const dependency: CtemSignal = {
  id: ID,
  trigger_path: "code_flaw",
  trigger_path_label: "Code or dependency flaw",
  lens: "inside",
  source: { provider: "aikido", external_id: "7" },
  subject: { kind: "code", ref: "lodash", label: "lodash" },
  observed_at: "2026-11-22T05:00:00Z",
  first_seen_at: "2026-11-22T05:00:00Z",
  last_seen_at: "2026-11-22T05:00:00Z",
  seen_count: 1,
  evidence: {
    event: "issue.open.created",
    type: "open_source",
    severity: "critical",
    code_repo_name: "filigran/portal",
    affected_package: "lodash",
    installed_version: "4.17.20",
    fixed_version: "4.17.21",
    assets: ["filigran/portal", "991"],
  },
  state: "confirmed",
  decision: { version: 1, outcome: "in_scope" },
  case_id: null,
  recorded_via: "listener",
};

function renderTrace(signal: CtemSignal) {
  get.mockResolvedValue(signal);
  renderWithProviders(
    <MemoryRouter initialEntries={[`/ctem/signals?signal=${ID}`]}>
      <SignalTraceBody id={ID} />
    </MemoryRouter>,
  );
  return screen.findByTestId("ctem-signal-facts");
}

afterEach(() => get.mockReset());

describe("aikido signal facts", () => {
  it("names the source by its integration name when it is not a CTEM product", () => {
    expect(signalProviderName("aikido")).toBe("Aikido Security");
    expect(signalProviderName("opencti")).toBe("OpenCTI");
    expect(signalProviderName("unknown-source")).toBe("unknown-source");
  });

  it("reads the finding facts of an Aikido signal only", () => {
    expect(aikidoFacts("aikido", dependency.evidence).map((f) => f.key)).toEqual(["type", "repository", "package"]);
    expect(aikidoFacts("aikido", { container_repo_name: "ghcr.io/filigran/app", fixed_version: "2" })).toEqual([
      { key: "image", name: "ghcr.io/filigran/app" },
      { key: "fixed", fixed: "2" },
    ]);
    expect(aikidoFacts("aikido", { sla_breached: true })).toEqual([{ key: "sla" }]);
    expect(aikidoFacts("opencti", dependency.evidence)).toEqual([]);
    expect(aikidoFacts("aikido", null)).toEqual([]);
  });

  it("the trace shows the dependency in named rows and keeps them out of the raw evidence", async () => {
    const facts = await renderTrace(dependency);
    expect(within(facts).getByText("Aikido Security")).toBeTruthy();
    expect(within(facts).getByText("Dependency")).toBeTruthy();
    expect(within(facts).getByText("filigran/portal")).toBeTruthy();
    expect(within(facts).getByText("lodash 4.17.20, fixed in 4.17.21")).toBeTruthy();
    const evidence = within(facts).getByTestId("ctem-signal-evidence");
    expect(evidence).toHaveTextContent("severity");
    expect(evidence).not.toHaveTextContent("code_repo_name");
    expect(evidence).not.toHaveTextContent("affected_package");
  });

  it("a leaked secret names its type and repository, an SLA breach is said", async () => {
    const facts = await renderTrace({
      ...dependency,
      trigger_path: "credential_leak",
      trigger_path_label: "Credential leak",
      evidence: { event: "issue.sla.breached", type: "leaked_secret", code_repo_name: "filigran/portal", affected_file: ".env", sla_breached: true },
    });
    expect(within(facts).getByText("Leaked secret")).toBeTruthy();
    expect(within(facts).getByText("Breached in the source")).toBeTruthy();
    expect(within(facts).getByTestId("ctem-signal-evidence")).toHaveTextContent(".env");
  });
});
