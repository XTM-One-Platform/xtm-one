// Chat images and file links as a reader meets them.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { renderWithProviders } from "@/testing/providers";
import { ChatMarkdown } from "./ChatMarkdown";

const ID = "8bf09217-bdf2-4d3a-ae6d-7912a28879da";

beforeEach(() => {
  localStorage.setItem("copilot_token", "token");
});

afterEach(() => {
  localStorage.removeItem("copilot_token");
  vi.unstubAllGlobals();
});

describe("ChatMarkdown images", () => {
  it("opens an image in a named modal dialog that Escape closes, focus back on the opener (A1)", async () => {
    const png = "data:image/png;base64,iVBORw0KGgo=";
    renderWithProviders(<ChatMarkdown content={`![Q3 revenue](${png})`} />);

    const expand = screen.getByRole("button", { name: "Expand" });
    expand.focus();
    fireEvent.click(expand);

    const dialog = screen.getByRole("dialog", { name: "Q3 revenue" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(expand).toHaveFocus();
  });

  it("keeps the focus inside the lightbox on Tab and Shift+Tab (B1)", () => {
    renderWithProviders(<ChatMarkdown content="![Q3 revenue](data:image/png;base64,iVBORw0KGgo=)" />);
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));

    const dialog = screen.getByRole("dialog", { name: "Q3 revenue" });
    const close = screen.getByRole("button", { name: "Close" });
    fireEvent.keyDown(close, { key: "Tab" });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(close).toHaveFocus();
  });

  it("gives the focus back to the Expand button when Close is clicked (B2)", () => {
    renderWithProviders(<ChatMarkdown content="![Q3 revenue](data:image/png;base64,iVBORw0KGgo=)" />);
    const expand = screen.getByTestId("chat-image-expand");
    expand.focus();
    fireEvent.click(expand);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-image-expand")).toHaveFocus();
  });

  it("names the lightbox of a chart with no alt text after the chart (A1)", () => {
    renderWithProviders(<ChatMarkdown content="![](data:image/png;base64,iVBORw0KGgo=)" />);
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(screen.getByRole("dialog", { name: "Generated chart" })).toBeInTheDocument();
  });

  it("names a chart with no alt text in the reader's language, image and lightbox", () => {
    renderWithProviders(<ChatMarkdown content="![](data:image/png;base64,iVBORw0KGgo=)" />, { locale: "fr" });
    expect(screen.getByRole("img", { name: "Graphique généré" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("chat-image-expand"));
    expect(screen.getByRole("dialog", { name: "Graphique généré" })).toBeInTheDocument();
    expect(screen.queryByText("Generated chart")).not.toBeInTheDocument();
  });

  it("offers an Expand button on a chart and on an API image", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(new Blob(["png"], { type: "image/png" }), { status: 200 })),
    );
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
    renderWithProviders(
      <ChatMarkdown
        content={"![chart](data:image/png;base64,iVBORw0KGgo=)\n\n![api](/api/v1/chat/images/q3.png)"}
      />,
    );
    await screen.findByRole("img", { name: "api" });
    expect(screen.getAllByRole("button", { name: "Expand" })).toHaveLength(2);
  });

  it("rings both Expand buttons with the scrim token, never ring-focus", () => {
    // Token pinned on the source, as ImageLightbox.test.tsx does: frontend/AGENTS.md
    // forbids class-name assertions on rendered elements.
    const source = readFileSync(join(process.cwd(), "src/components/chat/ChatMarkdown.tsx"), "utf8");
    const expandLines = source.split("\n").filter((line) => /\bbg-scrim\b.*\btext-on-scrim\b/.test(line));
    expect(expandLines).toHaveLength(2);
    for (const line of expandLines) {
      expect(line).toMatch(/focus-visible:ring-2\b/);
      expect(line).toMatch(/focus-visible:ring-on-scrim\b/);
      expect(line).not.toMatch(/ring-focus\b/);
    }
    expect(source.match(/focus-visible:ring-on-scrim\b/g)).toHaveLength(2);
  });

  it("shows a file:/// image's alt text, never a broken image (A2)", () => {
    renderWithProviders(<ChatMarkdown content="![local chart](file:///Users/me/chart.png)" />);
    expect(screen.getByText("[local chart: image could not be loaded]")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("keeps the model's alt text on an API image and names an empty one (A3)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(new Blob(["png"], { type: "image/png" }), { status: 200 })),
    );
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
    renderWithProviders(
      <ChatMarkdown
        content={"![Q3 revenue by region](/api/v1/chat/images/q3.png)\n\n![](/api/v1/chat/images/q4.png)"}
      />,
    );
    expect(await screen.findByRole("img", { name: "Q3 revenue by region" })).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "Generated image" })).toBeInTheDocument();
  });
});

describe("ChatMarkdown file links", () => {
  it("turns [text](file:<uuid>) into a link to the file's download path (A2)", async () => {
    const fetchMock = vi.fn(async () => new Response(new Blob(["pdf"]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:x", revokeObjectURL: () => {} }));
    renderWithProviders(<ChatMarkdown content={`Read [the report](file:${ID}).`} />);

    const link = screen.getByRole("link", { name: "the report" });
    expect(link.getAttribute("href")).toMatch(new RegExp(`/api/v1/chat/files/${ID}/download$`));

    fireEvent.click(link);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(new RegExp(`/chat/files/${ID}/download$`));
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer token");
  });

  it("keeps a multi-word file link as one link named by its full text", () => {
    // The wrapping between words is measured in the UX lab: happy-dom cannot
    // measure line breaks, and frontend/AGENTS.md forbids class-name assertions.
    renderWithProviders(<ChatMarkdown content={`See [scan results](file:${ID}).`} />);
    const link = screen.getByRole("link", { name: "scan results" });
    expect(link.getAttribute("href")).toMatch(new RegExp(`/api/v1/chat/files/${ID}/download$`));
    expect(screen.getAllByRole("link")).toHaveLength(1);
  });

  it("gives a file link with no text a visible, translated name", () => {
    renderWithProviders(<ChatMarkdown content={`See [](file:${ID}).`} />);
    const link = screen.getByRole("link", { name: "File" });
    expect(link.getAttribute("href")).toMatch(new RegExp(`/api/v1/chat/files/${ID}/download$`));
  });

  it("names a missing file by the link text, never by its id (B3)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "File not found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      })),
    );
    renderWithProviders(<ChatMarkdown content={`Read [the report](file:${ID}).`} />);

    fireEvent.click(screen.getByRole("link", { name: "the report" }));
    expect(await screen.findByText('"the report" no longer exists.')).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(ID))).not.toBeInTheDocument();
    // The backend's reason stays out of the toast.
    expect(screen.queryByText(/File not found/)).not.toBeInTheDocument();
  });
});

describe("ChatMarkdown code", () => {
  it("shows a one-line fence with no language as a block with a copy button (#12119)", () => {
    renderWithProviders(<ChatMarkdown content={"Run:\n\n```\nhelm list -a\n```"} />);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
    expect(screen.getByText("helm list -a").closest('[data-testid="chat-code-block"]')).not.toBeNull();
  });

  it("shows back-to-back bare one-line fences as one block each", () => {
    const content = [
      "**To see the real error** (one per line):",
      "",
      "```", "helm list -a", "```",
      "```", "helm history x --max 6", "```",
      "```", "kubectl get pods -o wide", "```",
    ].join("\n");
    renderWithProviders(<ChatMarkdown content={content} />);
    expect(screen.getAllByRole("button", { name: "Copy code" })).toHaveLength(3);
    for (const line of ["helm list -a", "helm history x --max 6", "kubectl get pods -o wide"]) {
      expect(screen.getByText(line).closest('[data-testid="chat-code-block"]')).not.toBeNull();
    }
  });

  it("keeps inline code inline", () => {
    renderWithProviders(<ChatMarkdown content="Run `helm list -a` first." />);
    expect(screen.queryByRole("button", { name: "Copy code" })).toBeNull();
    expect(screen.getByText("helm list -a").closest('[data-testid="chat-code-block"]')).toBeNull();
  });
});

describe("ChatMarkdown API images", () => {
  // Whatever host the model wrote in front of its path and however many lines
  // its alt text runs to, the panel's normalisation (`prepareMarkdown`) keeps
  // it one image, fetched by its platform path with the session.
  it("fetches a chat file's picture by its platform path, its multi-line alt text kept as one image", () => {
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    renderWithProviders(
      <ChatMarkdown content={"Here:\n\n![A chart of\nthe week's alerts](https://elsewhere.example/api/v1/chat/files/f-1/download)"} />,
    );
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/chat/files/f-1/download", expect.anything());
    expect(screen.queryByText(/the week's alerts/)).toBeNull();
  });
});
