// The sharing confirmation's buttons: a change that only takes access away
// reads "Keep sharing" against its own confirm, never against "Save and share".

import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CredentialSharingDialog, orphanedAgentsOf } from "@/components/ui/credential-sharing-notice";
import type { ResourcePreviewItem } from "@/types";

const emptyAudience = {
  everyone: false,
  people_count: 0,
  people: [],
  groups: [],
  public_link: false,
  anonymous_channels: [],
  mcp_key_holders: false,
  a2a_partners: false,
  webhook_callers: false,
};

const unshared = (orphaned: ResourcePreviewItem["orphaned_agents"]) =>
  ({
    resource: { kind: "integration", id: "i-1", name: "HR", provider: "hibob" },
    secret: { kind: "api_key", scopes: [], is_company: false, holder_id: "u-a", holder_name: "Alice" },
    via: { type: "group", id: "g-1", name: "Team" },
    gained: emptyAudience,
    lost: { ...emptyAudience, people_count: 1, people: [{ name: "Bob" }] },
    notes: [],
    opt_outs: [],
    orphaned_agents: orphaned,
  }) as unknown as ResourcePreviewItem;

const labels = { confirmLabel: "Save and share", unshareConfirmLabel: "Save and stop sharing" };

describe("CredentialSharingDialog buttons", () => {
  it("confirms an unshare with the unshare label", () => {
    renderWithProviders(<CredentialSharingDialog open items={[]} mode="unshare" onResolve={vi.fn()} {...labels} />);

    expect(screen.getByTestId("credential-sharing-confirm").textContent).toBe("Save and stop sharing");
    expect(screen.getByTestId("credential-sharing-cancel").textContent).toBe("Keep sharing");
  });

  it("confirms a share with the share label", () => {
    renderWithProviders(<CredentialSharingDialog open items={[]} mode="share" onResolve={vi.fn()} {...labels} />);

    expect(screen.getByTestId("credential-sharing-confirm").textContent).toBe("Save and share");
    expect(screen.getByTestId("credential-sharing-cancel").textContent).toBe("Keep things as they are");
  });

  it("lists the agents an unshare orphans and unbinds them on confirm", () => {
    const onResolve = vi.fn();
    const items = [
      unshared([
        { type: "agent", id: "a-1", name: "Bob's helper", visible: true, is_company_managed: false, owner_name: "Bob" },
        { type: "agent", id: null, name: null, visible: false, is_company_managed: false, owner_name: "Carol" },
      ]),
    ];
    renderWithProviders(<CredentialSharingDialog open items={items} mode="unshare" onResolve={onResolve} />);

    expect(screen.getByText("These agents would lose this integration")).toBeTruthy();
    expect(screen.getByText("Bob's helper, owned by Bob")).toBeTruthy();
    expect(screen.getByText("An agent you cannot open, owned by Carol")).toBeTruthy();
    fireEvent.click(screen.getByTestId("credential-sharing-confirm"));
    expect(onResolve).toHaveBeenCalledWith(true, true);
    expect(orphanedAgentsOf(items).map((o) => [o.agentId, o.integrationId])).toEqual([
      ["a-1", "i-1"],
      [null, "i-1"],
    ]);
  });

  it("lets the unbind be declined", () => {
    const onResolve = vi.fn();
    const items = [unshared([{ type: "agent", id: "a-1", name: "X", visible: true, is_company_managed: false, owner_name: "Bob" }])];
    renderWithProviders(<CredentialSharingDialog open items={items} mode="unshare" onResolve={onResolve} />);

    fireEvent.click(screen.getByLabelText("Unbind them too"));
    fireEvent.click(screen.getByTestId("credential-sharing-confirm"));
    expect(onResolve).toHaveBeenCalledWith(true, false);
  });

  it("shows no orphan section when nothing is orphaned", () => {
    renderWithProviders(<CredentialSharingDialog open items={[unshared([])]} mode="unshare" onResolve={vi.fn()} />);

    expect(screen.queryByTestId("credential-sharing-orphans")).toBeNull();
  });

  it("keeps a caller's single label when it names no unshare label", () => {
    renderWithProviders(<CredentialSharingDialog open items={[]} mode="unshare" confirmLabel="Remove" onResolve={vi.fn()} />);

    expect(screen.getByTestId("credential-sharing-confirm").textContent).toBe("Remove");
  });
});
