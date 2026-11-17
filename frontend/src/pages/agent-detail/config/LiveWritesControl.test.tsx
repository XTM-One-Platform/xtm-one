// OpenCTI Live writes per agent binding (#11958): the Writes control names the
// value a binding inherits, a Live override is offered only to who may edit the
// integration, and a save keeps only the overrides of bindings still bound.

import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { LiveWritesBadge, LiveWritesControl, integrationLiveWrites, prunedLiveWrites } from "./LiveWritesControl";

const ID = "11111111-1111-1111-1111-111111111111";
const OTHER = "22222222-2222-2222-2222-222222222222";

function renderControl(props: Partial<Parameters<typeof LiveWritesControl>[0]> = {}) {
  return renderWithProviders(
    <LiveWritesControl
      bindingKey={ID}
      label="OpenCTI production"
      inheritedLive={false}
      value="inherit"
      canSetLive
      onChange={vi.fn()}
      {...props}
    />,
  );
}

describe("LiveWritesControl", () => {
  it("names the inherited Off setting of the integration", () => {
    renderControl({ inheritedLive: false });
    expect(screen.getByText("Follows the integration, currently Draft only")).toBeTruthy();
    expect(screen.getByLabelText("Writes: OpenCTI production")).toBeTruthy();
  });

  it("names the inherited Live setting of the integration", () => {
    renderControl({ inheritedLive: true });
    expect(screen.getByText("Follows the integration, currently Live")).toBeTruthy();
  });

  it("says a per-user binding follows each user's integration", () => {
    renderControl({ bindingKey: "opencti", inheritedLive: null });
    expect(screen.getByText("Follows each user's integration")).toBeTruthy();
  });

  it("describes a Draft only override", () => {
    renderControl({ value: "off", inheritedLive: true });
    expect(screen.getByText("Writes are staged in a draft for review")).toBeTruthy();
  });

  it("describes a Live override", () => {
    renderControl({ value: "on" });
    expect(screen.getByText("Writes go to the live graph, no review")).toBeTruthy();
  });
});

describe("LiveWritesBadge", () => {
  it("shows nothing for an inheriting binding", () => {
    const { container } = renderWithProviders(<LiveWritesBadge mode={undefined} />);
    expect(container.textContent).toBe("");
  });

  it("shows the override", () => {
    renderWithProviders(<LiveWritesBadge mode="off" />);
    expect(screen.getByText("Writes: Draft only")).toBeTruthy();
  });
});

describe("integrationLiveWrites", () => {
  it("reads only 'on' as Live", () => {
    expect(integrationLiveWrites({ live_writes: "on" })).toBe(true);
    expect(integrationLiveWrites({ live_writes: "off" })).toBe(false);
    expect(integrationLiveWrites({})).toBe(false);
    expect(integrationLiveWrites(null)).toBe(false);
  });
});

describe("prunedLiveWrites", () => {
  it("keeps the overrides of bound OpenCTI instances and per-user bindings, multi-instance keys included", () => {
    expect(
      prunedLiveWrites(
        { [OTHER]: "on", [ID]: "off", opencti: "on" },
        { opencti: [ID], "opencti#2": [OTHER] },
        ["opencti"],
      ),
    ).toEqual({ [ID]: "off", [OTHER]: "on", opencti: "on" });
  });

  it("drops the override of an unbound integration and answers null when none is left", () => {
    expect(prunedLiveWrites({ [OTHER]: "on" }, { opencti: [ID] }, [])).toBeNull();
    expect(prunedLiveWrites({ opencti: "off" }, {}, [])).toBeNull();
  });
});
