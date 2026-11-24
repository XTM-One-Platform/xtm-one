// The evaluation list and its Run Search explain themselves in place: a value
// says which score it is, and a filter says what it keeps.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders, testQueryClient } from "@/testing/providers";
import Evaluations from "./Evaluations";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

vi.mock("@/hooks/useAuth", () => ({
  useAuth: () => ({ user: { id: "me" }, isAdmin: true }),
}));

const ai = vi.hoisted(() => ({ off: false }));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => ai.off,
  useAiDisabledReason: () => (ai.off ? "AI is switched off on this platform" : null),
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);

const EVALUATION = {
  id: "e-1",
  user_id: "someone-else",
  name: "Alert triage",
  description: null,
  enabled: true,
  family: "PROSE",
  trigger_config: { schedule: null, on_agent_change: false },
  use_agent_default_model: false,
  model_ids: ["gpt-x", "gpt-y"],
  disabled_model_ids: ["gpt-y"],
  agent_ids: ["a-1"],
  entry_count: 12,
  is_company_managed: false,
  shared_group_ids: [],
  shared_user_ids: ["me"],
  has_active_runs: false,
  last_triggered_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  ai.off = false;
  get.mockImplementation(((path: string) =>
    Promise.resolve(path.startsWith("/evaluations") ? [EVALUATION] : [])) as typeof api.get);
  post.mockResolvedValue({
    runs: [
      {
        id: "r-1",
        evaluation_id: "e-1",
        agent_id: "a-1",
        model_id: "gpt-x",
        status: "completed",
        score: 81,
        speed_score: null,
        overall_score: null,
        f1_score: null,
        quality_score: 64,
        performance_score: null,
        created_at: "2026-10-25T10:00:00Z",
        tags: null,
        effective_tags: {},
        is_baseline: true,
      },
    ],
    count: 1,
  });
});

function renderPage(client = testQueryClient()) {
  return renderWithProviders(
    <MemoryRouter>
      <Evaluations />
    </MemoryRouter>,
    { client },
  );
}

describe("Evaluations", () => {
  it("names the score a Run Search value is, then defines it", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Search Runs/ }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Min score" }), { target: { value: "50" } });

    fireEvent.focus(await screen.findByText("81.0"));
    const tip = (await screen.findByRole("tooltip")).textContent;
    expect(tip).toMatch(/^Judge Quality: /);
    expect(tip).toContain("Scored by the LLM judge");
  });

  it("tags a placebo comparison's run in Run Search, and leaves an ordinary run untagged", async () => {
    post.mockResolvedValue({
      runs: [
        { id: "r-1", evaluation_id: "e-1", agent_id: "a-1", model_id: "gpt-x", status: "completed", score: 81, created_at: "2026-10-25T10:00:00Z", tags: null },
        {
          id: "r-2", evaluation_id: "e-1", agent_id: "a-1", model_id: "gpt-y", status: "completed", score: 40, created_at: "2026-10-25T10:00:00Z", tags: null,
          arm: "no_target", placebo_target: { kind: "persona", skill_id: null }, placebo_comparison_id: "c-1",
        },
      ],
      count: 2,
    });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Search Runs/ }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Min score" }), { target: { value: "10" } });

    expect(await screen.findByText(/No target/)).toBeInTheDocument();
    expect(screen.getAllByText(/No target/)).toHaveLength(1);
  });

  it("says a shared evaluation can be shared directly, not only through a group", async () => {
    renderPage();

    fireEvent.focus((await screen.findByText("Shared")).closest('[tabindex="0"]')!);
    const tips = (await screen.findAllByRole("tooltip")).map((tip) => tip.textContent).join(" ");
    expect(tips).toContain("shared with you by its owner, directly or through a group");
  });

  it("says the model count includes disabled models, to a keyboard user", async () => {
    const user = userEvent.setup();
    renderPage();

    const models = (await screen.findByLabelText("Models")).closest('[tabindex="0"]');
    for (let presses = 0; presses < 30 && document.activeElement !== models; presses++) await user.tab();
    expect(document.activeElement).toBe(models);
    expect((await screen.findByRole("tooltip")).textContent).toContain("disabled ones included");
  });

  it("links the card through its name, with no button or explanation nested in the link", async () => {
    renderPage();

    const link = await screen.findByRole("link", { name: "Alert triage" });
    expect(link.getAttribute("href")).toMatch(/^\/evaluations\/[^/]+$/);
    expect(screen.getByRole("button", { name: "Copy evaluation" }).closest("a")).toBeNull();
    expect((await screen.findByLabelText("Models")).closest("a")).toBeNull();
  });

  it("keeps the full name on a card that carries every badge", async () => {
    const name = "OCR benchmark with a name longer than the card";
    get.mockImplementation(((path: string) =>
      Promise.resolve(
        path.startsWith("/evaluations")
          ? [{ ...EVALUATION, name, is_company_managed: true, has_active_runs: true }]
          : [],
      )) as typeof api.get);
    renderPage();

    const link = await screen.findByRole("link", { name });
    expect(link.getAttribute("title")).toBe(name);
    expect(screen.getByText("Company-managed")).toBeTruthy();
    expect(screen.getByText("Evaluating...")).toBeTruthy();
  });

  it("keeps a model no provider offers out of a Run selection, and says why", async () => {
    get.mockImplementation(((path: string) =>
      Promise.resolve(
        path.startsWith("/evaluations")
          ? [{ ...EVALUATION, model_ids: ["gpt-x", "gone-model"], disabled_model_ids: [], unavailable_model_ids: ["gone-model"] }]
          : [],
      )) as typeof api.get);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    const gone = screen.getByRole("button", { name: /gone-model/ });
    expect(gone).toBeDisabled();
    expect(gone).toHaveTextContent("Unavailable");
    fireEvent.focus(gone.closest('[tabindex="0"]')!);
    expect((await screen.findByRole("tooltip")).textContent).toContain("a launch that includes it is refused");

    const served = screen.getByRole("button", { name: "gpt-x" });
    await user.click(served);
    expect(served).toHaveAttribute("aria-pressed", "true");
  });

  it("offers the owner's models to add, read when the Run menu opens", async () => {
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1/available-models") return Promise.resolve([{ model: "owner-extra", provider: "openai" }]);
      if (path.startsWith("/settings/available-models")) return Promise.resolve([{ model: "mine", provider: "openai" }]);
      return Promise.resolve(path.startsWith("/evaluations") ? [EVALUATION] : []);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderPage();

    const options = await screen.findByRole("button", { name: "Open run options" });
    expect(get.mock.calls.map(([path]) => path)).not.toContain("/evaluations/e-1/available-models");
    await user.click(options);
    expect(await screen.findByRole("button", { name: /owner-extra/ })).toBeInTheDocument();
    expect(screen.queryByText("mine")).toBeNull();
  });

  it("runs an evaluation with no saved model on a model added from its Run menu", async () => {
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1/available-models") return Promise.resolve([{ model: "owner-extra", provider: "openai" }]);
      return Promise.resolve(path.startsWith("/evaluations") ? [{ ...EVALUATION, model_ids: [], disabled_model_ids: [] }] : []);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    await user.click(await screen.findByRole("button", { name: /owner-extra/ }));
    await user.click(screen.getByRole("button", { name: "Run Selected (1)" }));
    expect(post).toHaveBeenCalledWith("/evaluations/e-1/trigger", { model_ids: ["owner-extra"] });
  });

  it("drops a selected model from the selection once the server flags it", async () => {
    let flagged: string[] = [];
    get.mockImplementation(((path: string) =>
      Promise.resolve(
        path.startsWith("/evaluations")
          ? [{ ...EVALUATION, disabled_model_ids: [], unavailable_model_ids: flagged }]
          : [],
      )) as typeof api.get);
    const user = userEvent.setup();
    const { client } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    await user.click(screen.getByRole("button", { name: "gpt-x" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();

    flagged = ["gpt-x"];
    await client.invalidateQueries();
    await waitFor(() => expect(screen.getByRole("button", { name: /gpt-x/ })).toBeDisabled());
    expect(screen.getByRole("button", { name: /gpt-x/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("says which models a copy keeps that it cannot run", async () => {
    const warning = "Model gpt-x is kept but cannot run: no provider you can use offers it";
    post.mockImplementation(((path: string) =>
      Promise.resolve(path === "/evaluations/e-1/copy" ? { ...EVALUATION, id: "e-2", name: "Copy of Alert triage", warnings: [warning] } : {})) as typeof api.post);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Copy evaluation" }));
    expect(await screen.findByText(warning)).toBeInTheDocument();
  });

  it("names a model's provider in the Run menu as the evaluation page does", async () => {
    get.mockImplementation(((path: string) => {
      if (path === "/evaluations/e-1/available-models") return Promise.resolve([{ model: "owner-extra", provider: "openai" }]);
      return Promise.resolve(path.startsWith("/evaluations") ? [EVALUATION] : []);
    }) as typeof api.get);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    const row = await screen.findByRole("button", { name: /owner-extra/ });
    expect(within(row).getByText("OpenAI")).toBeInTheDocument();
    expect(within(row).queryByText("openai")).toBeNull();
  });

  // The server says why it refuses a launch, once: the app's own error toast stays quiet.
  it.each([
    ["Run evaluation now", [] as string[]],
    ["Run Selected (1)", ["gpt-x"]],
  ])("says once why the server refuses %s", async (button, picked) => {
    const reason = "Model gone-model is not offered by any provider of the evaluation's owner";
    get.mockImplementation(((path: string) =>
      Promise.resolve(path.startsWith("/evaluations") ? [{ ...EVALUATION, disabled_model_ids: [] }] : [])) as typeof api.get);
    post.mockRejectedValue(new Error(reason));
    const user = userEvent.setup();
    renderPage(testQueryClient({ appMutationCache: true }));

    if (picked.length) {
      await user.click(await screen.findByRole("button", { name: "Open run options" }));
      for (const model of picked) await user.click(screen.getByRole("button", { name: model }));
    }
    await user.click(await screen.findByRole("button", { name: button }));
    expect(await screen.findAllByText(reason)).toHaveLength(1);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("launches nothing from the Run menu while AI is off, and says why", async () => {
    ai.off = true;
    get.mockImplementation(((path: string) =>
      Promise.resolve(path.startsWith("/evaluations") ? [{ ...EVALUATION, disabled_model_ids: [] }] : [])) as typeof api.get);
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Open run options" }));
    await user.click(screen.getByRole("button", { name: "gpt-x" }));
    const runAll = screen.getByRole("button", { name: "Run All" });
    const runSelected = screen.getByRole("button", { name: "Run Selected (1)" });
    expect(runAll).toBeDisabled();
    expect(runSelected).toBeDisabled();
    fireEvent.focus(runSelected.closest('[tabindex="0"]')!);
    expect((await screen.findByRole("tooltip")).textContent).toContain("AI is switched off on this platform");
    expect(post).not.toHaveBeenCalled();
  });
});
