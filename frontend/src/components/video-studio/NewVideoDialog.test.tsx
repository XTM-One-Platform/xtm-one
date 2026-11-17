// "New video": any kind of video, and what the dialog shows is what both buttons create.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { renderWithProviders } from "@/testing/providers";
import type { VideoTemplateSummary } from "@/types";

import { NewVideoDialog } from "./NewVideoDialog";

const { mutateAsync, navigate, templates, loading } = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  navigate: vi.fn(),
  templates: [] as VideoTemplateSummary[],
  loading: { value: false },
}));

vi.mock("react-router-dom", async (importOriginal) => ({
  ...(await importOriginal<typeof import("react-router-dom")>()),
  useNavigate: () => navigate,
}));
vi.mock("@/hooks/api/useVideoStudioApi", () => ({
  useVideoTemplates: () => (loading.value ? { data: undefined, isLoading: true } : { data: templates, isLoading: false }),
  useCreateVideoProject: () => ({ mutateAsync, isPending: false }),
  useMusicLibrary: () => ({ data: [{ id: "deep-focus", name: "Deep Focus", mood: "calm", genre: "ambient tech" }], isLoading: false }),
}));
vi.mock("@/hooks/useToolAgents", () => ({
  useToolAgents: () => ({ agents: [], generalAssistantOffered: true, none: false, resolving: false, defaultAgentId: null }),
}));
vi.mock("@/hooks/useAiDisabled", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useAiDisabled")>()),
  useAiActionDisabled: () => false,
}));

function template(id: string, name: string, extra: Partial<VideoTemplateSummary> = {}): VideoTemplateSummary {
  return {
    id,
    name,
    description: null,
    kind: "video",
    is_default: false,
    is_builtin: true,
    supported_schemes: ["dark"],
    aspect_ratios: ["16:9", "9:16", "1:1", "4:5"],
    purposes: ["product_demo"],
    locked: [],
    intro: true,
    outro: true,
    background: null,
    shots: null,
    guidance: null,
    swatches: [],
    series: [],
    heading_font: null,
    ...extra,
  };
}

function open() {
  renderWithProviders(
    <MemoryRouter>
      <NewVideoDialog open onOpenChange={() => {}} />
    </MemoryRouter>,
  );
}

const created = () => mutateAsync.mock.calls[0]?.[0];

beforeEach(() => {
  loading.value = false;
  mutateAsync.mockReset().mockResolvedValue({ id: "v-1" });
  navigate.mockReset();
  templates.splice(0, templates.length, template("brand", "Brand Product Video", { is_default: true }), template("teaser", "Teaser", { purposes: ["social"], aspect_ratios: ["9:16", "1:1"] }));
});

describe("NewVideoDialog", () => {
  it("starts a blank video in free style, whatever the platform's default template", async () => {
    open();
    expect(screen.getByTestId("video.new.look").textContent).toContain("Free style");
    fireEvent.click(screen.getByRole("button", { name: /Start from a blank video/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(created()).toMatchObject({ freestyle: true, format: { aspect_ratio: "16:9" } });
    expect(created()).not.toHaveProperty("template_id");
    expect(navigate).toHaveBeenCalledWith("/videos/v-1", { state: {} });
  });

  it("makes a video just for fun in free style, asking for one", async () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "Just for fun" }));
    fireEvent.click(screen.getByRole("button", { name: /Use this example/ }));
    const request = screen.getByTestId("video.new.request").textContent ?? "";
    expect(request).toContain("Make this video just for fun");
    expect(request).toContain("cat astronaut");
    fireEvent.click(screen.getByRole("button", { name: /Create with the assistant/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(created()).toMatchObject({ freestyle: true });
    expect(navigate.mock.calls[0][1].state.send).toMatch(/^Make this video just for fun.*\n\nA 30-second video of a cat astronaut/s);
  });

  it("dresses a kind for work in the template made for it, in its format, and creates what it shows", async () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "Social teaser" }));
    expect(screen.getByTestId("video.new.look").textContent).toContain("Teaser");
    expect(screen.getByRole("button", { name: /Mobile/ }).getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: /Start from a blank video/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(created()).toMatchObject({ template_id: "teaser", format: { aspect_ratio: "9:16" } });
  });

  it("keeps a brief typed while the templates load once they arrive", () => {
    loading.value = true;
    const dialog = (
      <MemoryRouter>
        <NewVideoDialog open onOpenChange={() => {}} />
      </MemoryRouter>
    );
    const view = renderWithProviders(dialog);
    const brief = () => document.getElementById("vs-new-brief") as HTMLTextAreaElement;
    fireEvent.change(brief(), { target: { value: "A launch film for our new detection engine" } });
    loading.value = false;
    view.rerender(dialog);
    expect(brief()).toHaveValue("A launch film for our new detection engine");
  });

  it("keeps free style once chosen and offers the kind's template instead of imposing it", async () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.click(screen.getByRole("button", { name: /^Free style/ }));
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    fireEvent.click(screen.getByRole("button", { name: "Product demo" }));
    expect(screen.getByTestId("video.new.look").textContent).toContain("Free style");
    expect(screen.getByText(/Suggested for this kind of video: Brand Product Video/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Use it" }));
    expect(screen.getByTestId("video.new.look").textContent).toContain("Brand Product Video");
  });

  it("leaves every option to the assistant by default: nothing added to the request or the video", async () => {
    open();
    expect(screen.getByTestId("video.new.options-summary").textContent).toContain("the assistant suggests them");
    expect(screen.queryByTestId("video.new.options")).toBeNull();
    fireEvent.change(document.getElementById("vs-new-brief") as HTMLTextAreaElement, { target: { value: "Cats" } });
    expect(screen.getByTestId("video.new.request").textContent).not.toContain("Music:");
    fireEvent.click(screen.getByRole("button", { name: /Create with the assistant/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(created()).not.toHaveProperty("music");
    expect(created()).not.toHaveProperty("captions");
    expect(created()).not.toHaveProperty("pace");
    expect(navigate.mock.calls[0][1].state.send).toBe("Cats");
  });

  it("says each option set in the request and sets it on the video", async () => {
    open();
    fireEvent.change(document.getElementById("vs-new-brief") as HTMLTextAreaElement, { target: { value: "Cats" } });
    fireEvent.click(screen.getByTestId("video.new.options-toggle"));
    const options = screen.getByTestId("video.new.options");
    const chip = (group: string, name: string) =>
      Array.from(options.querySelectorAll<HTMLButtonElement>(`[aria-labelledby="${group}"] button`)).find((b) => b.textContent === name)!;
    fireEvent.click(chip("vs-new-music", "No music"));
    fireEvent.click(chip("vs-new-captions", "Burned in"));
    fireEvent.click(chip("vs-new-pace", "Calm"));
    fireEvent.change(screen.getByLabelText("Target length in seconds (optional)"), { target: { value: "30" } });
    expect(screen.getByTestId("video.new.options-summary").textContent).toContain("4 choices set");
    const request = screen.getByTestId("video.new.request").textContent ?? "";
    expect(request).toContain("Music: none.");
    expect(request).toContain("Captions: burned into the video.");
    expect(request).toContain("Length: about 30 seconds.");
    fireEvent.click(screen.getByRole("button", { name: /Create with the assistant/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(created()).toMatchObject({ music: { enabled: false }, captions: { enabled: true, burn_in: true }, pace: "calm" });
    expect(navigate.mock.calls[0][1].state.send).toBe("Cats\n\nMusic: none.\nCaptions: burned into the video.\nLength: about 30 seconds.\nPace: calm.");
  });

  it("marks a length outside 5 to 600 seconds invalid and keeps it out of the request (UX #11540 r1 B1)", () => {
    open();
    fireEvent.change(document.getElementById("vs-new-brief") as HTMLTextAreaElement, { target: { value: "Cats" } });
    fireEvent.click(screen.getByTestId("video.new.options-toggle"));
    const seconds = screen.getByLabelText("Target length in seconds (optional)") as HTMLInputElement;
    expect(seconds.getAttribute("aria-invalid")).toBeNull();
    for (const value of ["2", "900", "12.5"]) {
      fireEvent.change(seconds, { target: { value } });
      expect(seconds.value).toBe(value);
      expect(seconds.getAttribute("aria-invalid")).toBe("true");
      expect(document.getElementById(seconds.getAttribute("aria-describedby") ?? "")?.textContent).toBe("Enter a whole number of seconds from 5 to 600.");
      expect(screen.getByTestId("video.new.request").textContent).not.toContain("Length:");
      expect(screen.getByTestId("video.new.options-summary").textContent).toContain("the assistant suggests them");
    }
    fireEvent.change(seconds, { target: { value: "600" } });
    expect(seconds.getAttribute("aria-invalid")).toBeNull();
    expect(screen.getByTestId("video.new.request").textContent).toContain("Length: about 600 seconds.");
    fireEvent.click(screen.getByRole("button", { name: "Let the assistant suggest", pressed: false }));
    expect(seconds.value).toBe("");
    expect(screen.getByTestId("video.new.request").textContent).not.toContain("Length:");
  });

  it("labels every extra field above it, not by its placeholder alone (UX #11540 r1 B2)", () => {
    open();
    fireEvent.click(screen.getByTestId("video.new.options-toggle"));
    const options = screen.getByTestId("video.new.options");
    const chip = (group: string, name: string) =>
      Array.from(options.querySelectorAll<HTMLButtonElement>(`[aria-labelledby="${group}"] button`)).find((b) => b.textContent === name)!;
    fireEvent.click(chip("vs-new-narration", "With narration"));
    for (const name of ["Language (optional)", "Voice (optional)", "Target length in seconds (optional)"]) {
      const field = screen.getByLabelText(name);
      expect(document.getElementById(field.getAttribute("aria-labelledby") ?? "")?.textContent).toBe(name);
    }
    fireEvent.click(chip("vs-new-music", "Composed by AI"));
    expect(screen.getByLabelText("Mood")).toBeTruthy();
    expect(screen.getByText("Mood")).toBeTruthy();
  });

  it("gives Track, Mood, Language and Voice one help line each, named by the field (UX #11540 r2 B1)", () => {
    open();
    fireEvent.click(screen.getByTestId("video.new.options-toggle"));
    const options = screen.getByTestId("video.new.options");
    const chip = (group: string, name: string) =>
      Array.from(options.querySelectorAll<HTMLButtonElement>(`[aria-labelledby="${group}"] button`)).find((b) => b.textContent === name)!;
    // The help row is the first id the field names; a design-system Input's row also holds its N/max counter.
    const helpOf = (field: Element) => document.getElementById((field.getAttribute("aria-describedby") ?? "").split(" ")[0])?.textContent;
    fireEvent.click(chip("vs-new-narration", "With narration"));
    expect(helpOf(screen.getByLabelText("Language (optional)"))).toContain("Empty lets the assistant use the language of the brief.");
    expect(helpOf(screen.getByLabelText("Voice (optional)"))).toContain("Empty lets the assistant pick a voice that fits the brief.");
    fireEvent.click(chip("vs-new-music", "Composed by AI"));
    expect(helpOf(screen.getByLabelText("Mood"))).toContain("The assistant composes a track from this line.");
    fireEvent.click(chip("vs-new-music", "Built-in track"));
    const track = options.querySelector('[aria-labelledby="vs-new-music-track-label"]')!;
    expect(helpOf(track)).toBe("A built-in track from the studio library, played under the video.");
  });

  it("puts the help of Mood, Language and Voice in the Input's helper row, beside its counter (UX #11540 r3 B1)", () => {
    open();
    fireEvent.click(screen.getByTestId("video.new.options-toggle"));
    const options = screen.getByTestId("video.new.options");
    const chip = (group: string, name: string) =>
      Array.from(options.querySelectorAll<HTMLButtonElement>(`[aria-labelledby="${group}"] button`)).find((b) => b.textContent === name)!;
    fireEvent.click(chip("vs-new-narration", "With narration"));
    fireEvent.click(chip("vs-new-music", "Composed by AI"));
    const cases: Array<[string, string, string]> = [
      ["Mood", "The assistant composes a track from this line.", "0/300"],
      ["Language (optional)", "Empty lets the assistant use the language of the brief.", "0/60"],
      ["Voice (optional)", "Empty lets the assistant pick a voice that fits the brief.", "0/120"],
    ];
    for (const [label, help, counter] of cases) {
      const field = screen.getByLabelText(label);
      const row = document.getElementById(`${field.id}-helper`)!;
      expect(field.getAttribute("aria-describedby")?.split(" ")).toContain(row.id);
      expect(row.textContent).toBe(`${help}${counter}`);
      // No second help line of its own under the counter.
      expect(document.getElementById(`${field.id}-help`)).toBeNull();
    }
  });
});
