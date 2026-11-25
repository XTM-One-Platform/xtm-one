// SPDX-License-Identifier: Apache-2.0

// An empty History renders one way in both views: the same centred block, so switching
// the view never moves the sentence (UX round 1 on #2008, B1).
import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { renderWithProviders } from "@/testing/providers";
import { CaseAuditTrail } from "./CaseAuditTrail";

function renderEmpty() {
  return renderWithProviders(
    <MemoryRouter>
      <CaseAuditTrail
        rows={[]}
        phases={undefined}
        currentPhase={null}
        renderEvent={() => null}
        renderRun={() => null}
        actorName={() => ""}
        actionText={() => ""}
        changeText={() => null}
        personName={() => null}
        backTo="/"
      />
    </MemoryRouter>,
  );
}

// The wrapper of the empty line, named by its test id: both views render the same block.
const containerOf = () => screen.getByTestId("ctem-audit-empty").parentElement?.getAttribute("data-testid");

describe("CaseAuditTrail empty state (#2008, B1)", () => {
  it("renders the empty line in the same container in the Table and the Timeline", async () => {
    renderEmpty();
    expect(containerOf()).toBe("ctem-audit-empty-block");

    await userEvent.click(screen.getByRole("radio", { name: "Timeline" }));
    expect(screen.getByTestId("ctem-case-history")).toBeInTheDocument();
    expect(containerOf()).toBe("ctem-audit-empty-block");
  });
});
