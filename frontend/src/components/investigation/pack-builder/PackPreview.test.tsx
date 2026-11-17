// The pack builder's preview shows a step's label as its author wrote it, and
// translates the built-in report action.

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { testQueryClient } from "@/testing/providers";
import type { InvestigationPackFields, PackPreview as PackPreviewResult } from "@/types";
import { PackPreview } from "./PackPreview";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { post: vi.fn() },
}));

function action(slug: string, label: string, produces_report = false) {
  return { slug, label, description: "", icon: "target", produces_report, servable: true, sources: [] };
}

async function preview(written: string) {
  const result: PackPreviewResult = {
    subject: "example.com",
    subject_type: "domain",
    reachable: true,
    description: "",
    actions: [action("step:a", written), action("write_report", "Write the cited report", true)],
  };
  vi.mocked(api.post).mockResolvedValue(result);
  render(
    <QueryClientProvider client={testQueryClient()}>
      <IntlProvider
        locale="fr"
        messages={{ "Write the cited report": "Rédiger le rapport cité" }}
        onError={() => {}}
      >
        <MemoryRouter>
          <PackPreview draft={{} as InvestigationPackFields} slug={null} dirty={false} />
        </MemoryRouter>
      </IntlProvider>
    </QueryClientProvider>,
  );
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "example.com" } });
  fireEvent.submit(screen.getByRole("textbox").closest("form") as HTMLFormElement);
  return screen.findByText("Rédiger le rapport cité");
}

describe("PackPreview", () => {
  it.each([["Analyst's {x}"], ["Don't '{skip}' this"], ["It''s done"]])(
    "renders a step's label as written and the report action translated: %s",
    async (written) => {
      await preview(written);
      const labels = [...document.querySelectorAll("[data-pack-preview] li .font-medium")].map((el) => el.textContent);
      expect(labels).toEqual([written, "Rédiger le rapport cité"]);
    },
  );
});
