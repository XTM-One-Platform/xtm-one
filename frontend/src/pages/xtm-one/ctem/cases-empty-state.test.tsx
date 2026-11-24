// SPDX-License-Identifier: Apache-2.0
// The empty Cases list and board (#12139): "No open exposure on your software" is
// an all-clear only when every product answered; a product that could not be
// read is named instead, on the list page and on the board. The API sends
// product ids ("openaev"), so the mocks do too and the text shows display names.

import { afterEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import CtemCases from "../CtemCases";
import { CasesBoard } from "./CasesBoard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn() },
}));

const get = vi.mocked(api.get);
const ZERO = { needs_attention: 0, in_progress: 0, resolved: 0, accepted_risk: 0 };

function list(detailsUnavailable: string[], reasons?: Record<string, string>) {
  return { vertical: "cyber", counts: ZERO, cases: [], details_unavailable: detailsUnavailable, details_unavailable_reasons: reasons };
}

afterEach(() => get.mockReset());

describe("CasesBoard empty state", () => {
  it("says no open exposure when every product answered", async () => {
    get.mockResolvedValue(list([]));
    renderWithProviders(<MemoryRouter><CasesBoard /></MemoryRouter>);
    const empty = await screen.findByTestId("ctem-cases-board-empty");
    expect(empty.textContent).toBe("No open exposure on your software");
  });

  it("names the products that could not be read instead of the all-clear", async () => {
    get.mockResolvedValue(list(["opencti", "openaev"]));
    renderWithProviders(<MemoryRouter><CasesBoard /></MemoryRouter>);
    const empty = await screen.findByTestId("ctem-cases-board-empty");
    expect(empty.textContent).toBe("OpenCTI and OpenAEV could not be read, so open exposures there may be missing.");
    expect(screen.queryByText("No open exposure on your software")).toBeNull();
  });
});

describe("CtemCases list empty state", () => {
  it("says no open exposure when every product answered", async () => {
    get.mockResolvedValue(list([]));
    renderWithProviders(<MemoryRouter><CtemCases /></MemoryRouter>);
    expect(await screen.findByText("No open exposure on your software")).toBeTruthy();
  });

  it("names the product that could not be read instead of the all-clear", async () => {
    get.mockResolvedValue(list(["openaev"]));
    renderWithProviders(<MemoryRouter><CtemCases /></MemoryRouter>);
    expect(await screen.findByText("OpenAEV could not be read, so open exposures there may be missing.")).toBeTruthy();
    expect(screen.queryByText("No open exposure on your software")).toBeNull();
  });

  it("reads the sentence in French with the product named", async () => {
    get.mockResolvedValue(list(["opencti"]));
    renderWithProviders(<MemoryRouter><CtemCases /></MemoryRouter>, { locale: "fr" });
    // The French catalog loads with the provider: give the first render more than the default second.
    expect(await screen.findByText(/^OpenCTI n'a pas pu être lu\s:\sdes expositions ouvertes peuvent manquer\.$/, undefined, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByText("Aucune exposition ouverte sur votre parc")).toBeNull();
  });

  // #1852: the reason of each failed read is said in plain words, never as a missing right.
  it.each([
    ["rejected", "OpenCTI rejected your sign-in token, so open exposures there may be missing."],
    ["too_large", "OpenCTI could not answer because the query was too large, so open exposures there may be missing."],
    ["timeout", "OpenCTI did not answer in time, so open exposures there may be missing."],
    ["not_connected", "No OpenCTI connection could be used for this read, so open exposures there may be missing."],
    ["unavailable", "OpenCTI could not be read, so open exposures there may be missing."],
  ])("says why OpenCTI did not answer (%s)", async (reason, sentence) => {
    get.mockResolvedValue(list(["opencti"], { opencti: reason }));
    renderWithProviders(<MemoryRouter><CtemCases /></MemoryRouter>);
    expect(await screen.findByText(sentence)).toBeTruthy();
  });

  it("names each product with its own reason", async () => {
    get.mockResolvedValue(list(["opencti", "openaev"], { opencti: "too_large", openaev: "timeout" }));
    renderWithProviders(<MemoryRouter><CasesBoard /></MemoryRouter>);
    const empty = await screen.findByTestId("ctem-cases-board-empty");
    expect(empty.textContent).toBe(
      "OpenCTI could not answer because the query was too large, so open exposures there may be missing. OpenAEV did not answer in time, so open exposures there may be missing.",
    );
  });

  it("agrees the French verb with two products", async () => {
    get.mockResolvedValue(list(["opencti", "openaev"]));
    renderWithProviders(<MemoryRouter><CtemCases /></MemoryRouter>, { locale: "fr" });
    expect(await screen.findByText(/^OpenCTI et OpenAEV n'ont pas pu être lus\s:\sdes expositions ouvertes peuvent manquer\.$/, undefined, { timeout: 5000 })).toBeTruthy();
  });
});
