/**
 * The "From chat, estimated" card (#10544 S5) - `yarn test:unit`. It reads its
 * own series, apart from the automation report; it names the agent and the
 * person of every line; only a counted line offers "Adjust", whose dialog
 * sends minutes, refuses an out-of-range value with aria-invalid and returns a
 * corrected line to its estimate; a failed read stays in the card.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { RoiChatLine, RoiChatSeries } from "@/hooks/api/useRoiChatApi";
import { roiChatPath } from "@/hooks/api/useRoiChatApi";
import en from "@/i18n/lang/en.json";
import es from "@/i18n/lang/es.json";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import { api } from "@/lib/api";
import { capTipText } from "@/lib/tip-text";
import { renderWithProviders } from "@/testing/providers";

import { CutText, hoursToMinutes, lineStatus, RoiChatSeriesCard } from "./RoiChatSeriesCard";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), put: vi.fn() },
}));

const get = vi.mocked(api.get);
const put = vi.mocked(api.put);

function line(over: Partial<RoiChatLine> = {}): RoiChatLine {
  return {
    id: "l-1",
    kind: "presentation",
    deliverable_id: "d-1",
    title: "All Hands",
    user_id: "u-1",
    user_name: "Ada Lovelace",
    agent_id: "a-1",
    agent_name: "Deck builder",
    conversation_id: "c-1",
    size: 30,
    size_label: "slides",
    signals: ["exported"],
    counted: true,
    last_edit_at: "2026-11-09T09:00:00Z",
    deleted: false,
    backfilled: false,
    unit: "executive_deck_built",
    estimate: { manual_min_low: 1200, user_min: 180, subtracted_min: 225, cap_min: 1440, counted_min: 975 },
    adjusted_min: null,
    adjusted_at: null,
    minutes: 975,
    hours: 16.25,
    value: 1218.75,
    can_adjust: true,
    ...over,
  };
}

function series(lines: RoiChatLine[]): RoiChatSeries {
  const counted = lines.filter((l) => l.counted);
  return {
    label: "From chat, estimated",
    window: "30d",
    currency: "USD",
    rate_label: "assumption",
    totals: {
      deliverables: counted.length,
      pending: lines.length - counted.length,
      corrected: counted.filter((l) => l.adjusted_min !== null).length,
      minutes: counted.reduce((s, l) => s + l.minutes, 0),
      hours: counted.reduce((s, l) => s + l.hours, 0),
      value: counted.reduce((s, l) => s + (l.value ?? 0), 0),
    },
    by_kind: {
      presentation: { deliverables: counted.length, minutes: 975 },
      report: { deliverables: 0, minutes: 0 },
      video: { deliverables: 0, minutes: 0 },
      file: { deliverables: 0, minutes: 0 },
    },
    lines,
    total_lines: lines.length,
    offset: 0,
    limit: 200,
    method: "",
  };
}

function renderCard(scope: Parameters<typeof RoiChatSeriesCard>[0]["scope"] = { kind: "organization" }) {
  return renderWithProviders(
    <MemoryRouter>
      <RoiChatSeriesCard scope={scope} window="30d" urlKey="chat_" />
    </MemoryRouter>,
  );
}

afterEach(() => {
  get.mockReset();
  put.mockReset();
});

describe("the chat series path", () => {
  it("reads every scope apart from the automation report", () => {
    expect(roiChatPath({ kind: "organization" }, "30d")).toBe("/roi/chat?window=30d&limit=200");
    expect(roiChatPath({ kind: "mine" }, "7d")).toBe("/roi/chat?window=7d&limit=200&mine=true");
    expect(roiChatPath({ kind: "fleet", id: "f-1" }, "30d")).toBe("/roi/chat?window=30d&limit=200&fleet_id=f-1");
    expect(roiChatPath({ kind: "agent", id: "a-1" }, "90d")).toBe("/roi/chat?window=90d&limit=200&agent_id=a-1");
  });
});

describe("hoursToMinutes", () => {
  it("turns hours into minutes and refuses what is out of range", () => {
    expect(hoursToMinutes("16.25")).toBe(975);
    expect(hoursToMinutes("2,5")).toBe(150);
    expect(hoursToMinutes("0")).toBe(0);
    expect(hoursToMinutes("")).toBeNull();
    expect(hoursToMinutes("-1")).toBeNull();
    expect(hoursToMinutes("721")).toBeNull();
    expect(hoursToMinutes("abc")).toBeNull();
  });

  it("tells pending, counted and corrected lines apart", () => {
    expect(lineStatus(line({ counted: false }))).toBe("pending");
    expect(lineStatus(line())).toBe("counted");
    expect(lineStatus(line({ adjusted_min: 600 }))).toBe("corrected");
  });
});

describe("RoiChatSeriesCard", () => {
  it("shows the series with its totals, the agent and the person of each line", async () => {
    get.mockResolvedValue(series([line(), line({ id: "l-2", title: "Board memo", kind: "file", counted: false, minutes: 0, hours: 0, value: 0 })]));
    renderCard();
    expect(await screen.findByText("All Hands")).toBeTruthy();
    expect(screen.getByRole("heading", { name: /From chat, estimated/ })).toBeTruthy();
    expect(screen.getByTestId("roi-chat-totals").textContent).toBe("1 deliverable counted, 1 waiting for use");
    expect(screen.getAllByText("Deck builder")).toHaveLength(2);
    expect(screen.getAllByText("Ada Lovelace")).toHaveLength(2);
    expect(screen.getByText("Waiting for use")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/roi/chat?window=30d&limit=200");
  });

  it("says 0 counted with a plural at 0 and 1", async () => {
    get.mockResolvedValue(series([line({ counted: false, minutes: 0, hours: 0, value: 0 })]));
    renderCard();
    await waitFor(() => expect(screen.getByTestId("roi-chat-totals").textContent).toBe("0 deliverables counted, 1 waiting for use"));
  });

  it("offers Adjust only on a line the reader may correct", async () => {
    get.mockResolvedValue(series([line(), line({ id: "l-2", title: "Colleague deck", can_adjust: false })]));
    renderCard({ kind: "mine" });
    expect(await screen.findByRole("button", { name: "Adjust All Hands" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Adjust Colleague deck" })).toBeNull();
  });

  it("shows a non-administrator's organization read as totals and kinds, with no table", async () => {
    const empty = series([]);
    get.mockResolvedValue({
      ...empty,
      aggregate_only: true,
      totals: { ...empty.totals, deliverables: 1, hours: 16.25, value: 1218.75 },
      by_kind: { ...empty.by_kind, presentation: { deliverables: 1, minutes: 975 } },
    });
    renderCard();
    expect(await screen.findByTestId("roi-chat-kinds")).toBeTruthy();
    expect(screen.getByText("Deck")).toBeTruthy();
    expect(screen.getByText("Each deliverable is listed only to the person who made it and to administrators.")).toBeTruthy();
    expect(screen.queryByTestId("roi-chat-row")).toBeNull();
    expect(screen.queryByRole("button", { name: /Adjust/ })).toBeNull();
  });

  it("offers Adjust only on a counted line", async () => {
    get.mockResolvedValue(series([line(), line({ id: "l-2", title: "Draft", counted: false, minutes: 0, hours: 0, value: 0 })]));
    renderCard();
    expect(await screen.findByRole("button", { name: "Adjust All Hands" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Adjust Draft" })).toBeNull();
  });

  it("sends the corrected hours as minutes", async () => {
    get.mockResolvedValue(series([line()]));
    put.mockResolvedValue({ id: "l-1", adjusted_min: 600, adjusted_at: "2026-11-10T09:00:00Z" });
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Adjust All Hands" }));
    const field = screen.getByRole("spinbutton", { name: "Hours saved" });
    fireEvent.change(field, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/roi/chat/records/l-1", { minutes: 600 }));
  });

  it("refuses an out-of-range value on the field itself", async () => {
    get.mockResolvedValue(series([line()]));
    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Adjust All Hands" }));
    const field = screen.getByRole("spinbutton", { name: "Hours saved" });
    fireEvent.change(field, { target: { value: "-3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(field.getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByText("Enter a number of hours between 0 and 720.")).toBeTruthy();
    expect(put).not.toHaveBeenCalled();
  });

  it("returns a corrected line to its estimate", async () => {
    get.mockResolvedValue(series([line({ adjusted_min: 600, minutes: 600, hours: 10 })]));
    put.mockResolvedValue({ id: "l-1", adjusted_min: null, adjusted_at: null });
    renderCard();
    expect(await screen.findByText("Corrected")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Adjust All Hands" }));
    fireEvent.click(screen.getByRole("button", { name: "Back to the estimate" }));
    await waitFor(() => expect(put).toHaveBeenCalledWith("/roi/chat/records/l-1", { minutes: null }));
  });

  it("keeps a failed read inside the card", async () => {
    get.mockRejectedValue(Object.assign(new Error("boom"), { status: 500 }));
    renderCard({ kind: "fleet", id: "f-1" });
    expect(await screen.findByText("The chat estimate could not be read.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});

/**
 * UX round 3 of #10681, B1: in fr and es the status badge ("En attente d'usage",
 * "A la espera de uso") pushed the table 64 and 51 px past its 802 px box at
 * 1080. The status column is sized by its widest badge, which never wraps, so no
 * locale's status label may be longer than the en one the layout fits with (the
 * lab render is the proof of the fit, this is the guard of its budget).
 */
describe("the status labels fit the en budget in every locale", () => {
  const keys = ["Waiting for use", "Counted", "Corrected"] as const;
  const budget = Math.max(...keys.map((key) => (en as Record<string, string>)[key].length));
  for (const [locale, catalog] of Object.entries({ en, es, fr, ja })) {
    it(`keeps every status label of ${locale} within ${budget} characters`, () => {
      for (const key of keys) {
        const label = (catalog as Record<string, string>)[key];
        expect(label, `${locale} "${key}"`).toBeTruthy();
        expect(label.length, `${locale} "${key}" = "${label}"`).toBeLessThanOrEqual(budget);
      }
    });
  }
});

/**
 * UX round 2 of #12240, B1: a Tip cuts any text past TIP_MAX_CHARS (capTipText),
 * so the card's explanation lost the line it names ("above" in en, the line and
 * section names in fr and es). Every locale must reach the reader whole.
 */
describe("the chat card explanation fits a Tip in every locale", () => {
  const key =
    "Decks, reports, videos and documents an agent finished in a chat, counted once used or 7 days after the last edit. Manual time (low bound) minus the chat time plus 25%. Same figure as the Chat (estimated) line of How this is computed.";
  for (const [locale, catalog] of Object.entries({ en, es, fr, ja })) {
    it(`shows the ${locale} explanation uncut, naming the Chat line and the section`, () => {
      const strings = catalog as Record<string, string>;
      const text = strings[key];
      expect(text, `${locale} "${key}"`).toBeTruthy();
      expect(capTipText(text), `${locale} length ${text.length}`).toBe(text);
      expect(text).toContain(strings["Chat (estimated)"]);
      expect(text).toContain(strings["How this is computed"]);
    });
  }
});

/** UX round 1 of #10681, B2: a cut cell is named by the DS tooltip, never a native title. */
describe("CutText", () => {
  function widths(scroll: number, client: number) {
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scroll);
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(client);
  }
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("is a plain span with no tab stop and no title when the text fits", () => {
    widths(100, 100);
    renderWithProviders(<CutText text="Short name" />);
    const node = screen.getByText("Short name");
    expect(node.getAttribute("title")).toBeNull();
    expect(node.getAttribute("tabindex")).toBeNull();
  });

  it("becomes a keyboard tab stop that opens the tooltip with the whole text when cut", async () => {
    widths(580, 256);
    const long = "A very long deliverable title that the column cannot show whole";
    renderWithProviders(<CutText text={long} />);
    const node = screen.getByText(long);
    expect(node.getAttribute("title")).toBeNull();
    await waitFor(() => expect(node.getAttribute("tabindex")).toBe("0"));
    fireEvent.focus(node);
    expect((await screen.findByRole("tooltip")).textContent).toContain(long);
  });

  /**
   * UX round 2 of #10681, B2: the text is cut only after mount (fonts, column
   * resize) and a detached span reads 0 wide, as in a browser. The first cut
   * wraps the span in the tooltip trigger, which remounts it: the span that is
   * mounted must be the one measured, so the tooltip stays on hover and focus.
   */
  it("keeps the tooltip when the text becomes cut after mount and the span remounts", async () => {
    const observers = new Set<{ cb: () => void; nodes: Set<Element> }>();
    class StubResizeObserver {
      entry: { cb: () => void; nodes: Set<Element> };
      constructor(cb: () => void) {
        this.entry = { cb, nodes: new Set() };
        observers.add(this.entry);
      }
      observe(target: Element) {
        this.entry.nodes.add(target);
      }
      unobserve(target: Element) {
        this.entry.nodes.delete(target);
      }
      disconnect() {
        observers.delete(this.entry);
      }
    }
    vi.stubGlobal("ResizeObserver", StubResizeObserver);
    let size = { scroll: 100, client: 100 };
    vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.isConnected ? size.scroll : 0;
    });
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
      return this.isConnected ? size.client : 0;
    });
    const fire = () =>
      act(() => {
        for (const entry of [...observers]) if (entry.nodes.size > 0) entry.cb();
      });
    const long = "A deliverable title cut once the column narrows";
    renderWithProviders(<CutText text={long} />);
    expect(screen.getByText(long).getAttribute("tabindex")).toBeNull();

    size = { scroll: 580, client: 256 };
    fire();
    fire();

    const node = screen.getByText(long);
    await waitFor(() => expect(node.getAttribute("tabindex")).toBe("0"));
    fireEvent.focus(node);
    expect((await screen.findByRole("tooltip")).textContent).toContain(long);
  });
});
