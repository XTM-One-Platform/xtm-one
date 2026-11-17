// The dataset library explains its concepts in place: a card's badges and
// counts, the imports, and the fields of the two creation dialogs, each reached with the
// keyboard as well as the mouse.

import { MemoryRouter } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type { Dataset, FamilyInfo } from "@/types";
import Datasets from "./Datasets";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

let aiEnabled = true;
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => aiEnabled,
}));

/** The wizard's pickers are positioned with floating-ui, which happy-dom does not lay out. */
beforeAll(() => {
  if (!("ResizeObserver" in globalThis)) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver;
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = vi.fn();
  }
});

const get = vi.mocked(api.get);

const FAMILIES: FamilyInfo[] = [
  {
    id: "CLASSIFICATION",
    label: "Classification",
    description: "Labels compared with a reference label set.",
    metric_keys: [],
    structured_output: true,
    extractor_output_kinds: [],
  },
];

function dataset(overrides: Partial<Dataset>): Dataset {
  return {
    id: "ds-1",
    user_id: "u-1",
    name: "Phishing",
    description: null,
    evaluation_instructions: null,
    output_schema: null,
    family: "CLASSIFICATION",
    label_vocabulary: null,
    needs_family: false,
    content_hash: null,
    current_version: 1,
    is_company_managed: false,
    is_read_only: false,
    shared_group_ids: null,
    evaluation_count: 2,
    entry_count: 5,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...overrides,
  } as Dataset;
}

beforeEach(() => {
  aiEnabled = true;
  get.mockReset();
  get.mockImplementation(async (path: string) => {
    if (path === "/evaluations/families") return FAMILIES;
    if (path === "/datasets") {
      return [
        dataset({ id: "ds-1", name: "Phishing", is_read_only: true }),
        dataset({ id: "ds-2", name: "Unsorted", family: null, needs_family: true, evaluation_count: 0 }),
      ];
    }
    if (path.startsWith("/datasets/hf-splits")) return { splits: [{ config: "default", split: "train", num_rows: 50 }] };
    if (path.startsWith("/datasets/hf-preview")) return { features: [{ name: "question" }, { name: "answer" }], rows: [] };
    return [];
  });
});

function renderPage() {
  renderWithProviders(
    <MemoryRouter>
      <Datasets />
    </MemoryRouter>,
  );
}

/** Tab alone until *target* has the focus, as a keyboard user reaches it. */
async function tabTo(user: ReturnType<typeof userEvent.setup>, target: Element) {
  for (let presses = 0; presses < 80 && document.activeElement !== target; presses++) await user.tab();
  expect(document.activeElement).toBe(target);
}

/** The focusable trigger an `InfoLabel` puts around a term. */
function termTrigger(text: string): Element {
  return screen.getAllByText(text)[0].closest('[tabindex="0"]')!;
}

describe("Datasets explains its concepts", () => {
  it.each([
    ["Read-only", "cannot be deleted, until read-only is turned off"],
    ["Needs family", "cannot run, replay or rescore until a family is set"],
    ["Classification", "Labels compared with a reference label set."],
    ["2 evaluations", "The number of evaluations linked to this dataset."],
  ])("explains a card's %j to a keyboard user", async (term, explanation) => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole("link", { name: "Phishing" });
    await screen.findAllByText("Classification");

    await tabTo(user, termTrigger(term));
    expect((await screen.findByRole("tooltip")).textContent).toContain(explanation);
  });

  it("names a card's export button and keeps its name a link to the dataset", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole("link", { name: "Phishing" })).toHaveAttribute("href", "/evaluations/datasets/ds-1");
    await tabTo(user, screen.getAllByRole("button", { name: "Export as ZIP" })[0]);
    expect((await screen.findByRole("tooltip")).textContent).toContain("Export as ZIP");
  });

  it("explains the HuggingFace import", async () => {
    const user = userEvent.setup();
    renderPage();

    await tabTo(user, await screen.findByRole("button", { name: "HuggingFace" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("each row becomes an entry");
  });

  it("disables both imports with AI off, and says why", async () => {
    aiEnabled = false;
    const user = userEvent.setup();
    renderPage();

    const zip = await screen.findByRole("button", { name: "Import ZIP" });
    expect(zip).toBeDisabled();
    expect(screen.getByRole("button", { name: "HuggingFace" })).toBeDisabled();
    await tabTo(user, zip.parentElement!);
    expect((await screen.findByRole("tooltip")).textContent).toContain("AI is disabled on this platform.");
  });

  it("names the new dataset's instructions Default Judge Instructions and says what replaces them", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    expect(screen.getByLabelText("Default Judge Instructions")).toBeInTheDocument();
    await tabTo(user, screen.getByRole("img", { name: "What the default judge instructions do" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "An evaluation's own judge instructions replace them",
    );
  });

  it("says a new dataset still needs an output family", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New Dataset" }));
    await tabTo(user, screen.getByRole("img", { name: "What a new dataset still needs" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain("Set one in its settings before an evaluation can run on it.");
  });

  it("says which layer the HuggingFace judge instructions column fills", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "HuggingFace" }));
    await user.type(screen.getByLabelText("HuggingFace Dataset ID"), "owner/mails");
    await user.click(screen.getByRole("button", { name: "Fetch" }));
    await user.click(await screen.findByRole("button", { name: "Next" }));

    expect(await screen.findByLabelText("Default Judge Instructions")).toBeInTheDocument();
    await tabTo(user, screen.getByRole("img", { name: "What the judge instructions column fills" }));
    expect((await screen.findByRole("tooltip")).textContent).toContain(
      "A column fills each entry's own judge instructions. Text written below instead fills the dataset's default judge instructions",
    );
  });
});
