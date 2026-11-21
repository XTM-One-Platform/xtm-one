// The Video Studio's libraries come a page at a time: the server pages,
// searches and filters, so every video, recording and shot stays reachable;
// the tab keeps its search, filters and page in the URL, with the page bar
// above and below the grid. The media are filtered by use and by date, and
// deleted in bulk, picked one by one or every one the filters match.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import VideoStudio from "./VideoStudio";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
// The posters and players fetch media tokens; the dialogs have their own data.
vi.mock("@/components/video-studio/VideoPlayer", () => ({ VideoPlayer: () => null, VideoPoster: () => null }));
vi.mock("@/components/video-studio/NewVideoDialog", () => ({ NewVideoDialog: () => null }));
vi.mock("@/components/video-studio/VideoShareDialog", () => ({ VideoShareDialog: () => null }));
vi.mock("@/components/video-studio/VideoTheater", () => ({ VideoTheater: () => <div data-testid="media.theater" /> }));

const VIDEOS = 30;
const SHOTS = 60;

function page(path: string): { offset: number; limit: number } {
  const params = new URLSearchParams(path.split("?")[1] ?? "");
  return { offset: Number(params.get("offset") ?? 0), limit: Number(params.get("limit") ?? 0) };
}

function answer(path: string): unknown {
  // Every video is the user's: the library lands on Mine.
  if (path.startsWith("/video-studio/projects/segment-counts"))
    return { mine: VIDEOS, company: 0, shared: 0, all: VIDEOS };
  if (path.startsWith("/video-studio/projects?")) {
    const { offset, limit } = page(path);
    const count = Math.max(0, Math.min(limit, VIDEOS - offset));
    const items = Array.from({ length: count }, (_, i) => ({
      id: `v-${offset + i}`,
      user_id: "u-1",
      title: `Explainer ${offset + i}`,
      template: null,
      scene_count: 1,
      latest_render: null,
      ready_render: null,
      can_manage: true,
      has_public_link: false,
    }));
    return { items, total: VIDEOS };
  }
  if (path.startsWith("/video-studio/assets?")) {
    const { offset, limit } = page(path);
    const count = Math.max(0, Math.min(limit, SHOTS - offset));
    const items = Array.from({ length: count }, (_, i) => ({
      kind: "clip",
      id: `c-${offset + i}`,
      name: null,
      prompt: `A cow in a blue coat, shot ${offset + i}`,
      status: "ready",
      keep: false,
      expired_at: null,
      expires_at: null,
      used_in: [],
      // The first shot is in a colleague's video the reader cannot open.
      in_use: offset + i === 0,
    }));
    return { items, total: SHOTS };
  }
  return {};
}

const requested = (path: string) => vi.mocked(api.get).mock.calls.some(([called]) => called === path);
const requestedLike = (pattern: RegExp) => vi.mocked(api.get).mock.calls.some(([called]) => pattern.test(called));

/** Confirm the open dialog with its Delete button; what it said, read before it closes. */
async function confirmDeletion(): Promise<string> {
  const dialog = await screen.findByRole("dialog");
  const said = dialog.textContent ?? "";
  fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
  return said;
}

function open(url: string) {
  renderWithProviders(
    <MemoryRouter initialEntries={[url]}>
      <Link to="/videos?q=booth">Search booth elsewhere</Link>
      <Routes>
        <Route path="/videos" element={<VideoStudio />} />
        <Route path="/videos/media" element={<VideoStudio />} />
      </Routes>
      <Location />
    </MemoryRouter>,
  );
}

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{`${pathname}${search}`}</output>;
}

describe("The studio tabs as path routes (#10203)", () => {
  it("leaves a former ?tab= link where it is: no redirect (#797)", async () => {
    open("/videos?tab=recordings&kind=shots");
    expect(await screen.findByTestId("location")).toHaveTextContent("/videos?tab=recordings&kind=shots");
  });

  it("moves between tabs by path", async () => {
    open("/videos");
    fireEvent.click(await screen.findByRole("button", { name: "Media" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/videos/media");
  });
});

beforeEach(() => {
  vi.mocked(api.get).mockReset();
  vi.mocked(api.get).mockImplementation(((path: string) => Promise.resolve(answer(path))) as typeof api.get);
  vi.mocked(api.post).mockReset();
  vi.mocked(api.post).mockResolvedValue({ deleted: 1, kept_in_use: 0 });
});

describe("The video library", () => {
  it("shows a page of videos and asks the server for the next one, from the one pager under the grid", async () => {
    open("/videos");

    expect(await screen.findByText("Explainer 0")).toBeInTheDocument();
    expect(requested("/video-studio/projects?vis=mine&limit=25&offset=0")).toBe(true);
    expect(screen.queryByText("Explainer 25")).toBeNull();
    // One pager, in the table's footer (#10215 criterion 6: no pager row above the list);
    // the total once, in the toolbar, never repeated under the rows (Sam, 11 Nov 2026).
    expect(screen.getAllByText("Page 1 of 2")).toHaveLength(1);
    expect(screen.getByTestId("data-table-total").textContent).toBe("30");
    expect(screen.queryByText(/Showing \d/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    expect(await screen.findByText("Explainer 25")).toBeInTheDocument();
    expect(requested("/video-studio/projects?vis=mine&limit=25&offset=25")).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(await screen.findByText("Explainer 0")).toBeInTheDocument();
  });

  it("sends a search to the server and starts again from the first page", async () => {
    open("/videos?page=1");
    expect(await screen.findByText("Explainer 25")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox", { name: "Search videos" }), { target: { value: "keynote" } });

    await waitFor(() => expect(requested("/video-studio/projects?search=keynote&vis=mine&limit=25&offset=0")).toBe(true));
  });

  it("sorts the whole library on the server and keeps the order in the URL", async () => {
    open("/videos?sort=title&dir=asc");
    expect(await screen.findByText("Explainer 0")).toBeInTheDocument();
    expect(requested("/video-studio/projects?vis=mine&limit=25&offset=0&sort=title&direction=asc")).toBe(true);
  });

  it("filters the videos by render state on the server and exports the rows as CSV", async () => {
    open("/videos?render=rendered");
    expect(await screen.findByText("Explainer 0")).toBeInTheDocument();
    expect(requested("/video-studio/projects?vis=mine&limit=25&offset=0&render=rendered")).toBe(true);
    // The scope counts follow the filter, so the switcher counts what the table lists.
    expect(requested("/video-studio/projects/segment-counts?render=rendered")).toBe(true);
    expect(screen.getByRole("button", { name: /Export/ })).toBeInTheDocument();
  });

  it("follows a search the URL changes by itself instead of writing the old one back", async () => {
    open("/videos?q=keynote");
    const box = await screen.findByRole("searchbox", { name: "Search videos" });
    expect(box).toHaveValue("keynote");
    await waitFor(() => expect(requested("/video-studio/projects?search=keynote&vis=mine&limit=25&offset=0")).toBe(true));

    fireEvent.click(screen.getByRole("link", { name: "Search booth elsewhere" }));

    await waitFor(() => expect(box).toHaveValue("booth"));
    await waitFor(() => expect(requested("/video-studio/projects?search=booth&vis=mine&limit=25&offset=0")).toBe(true));
    vi.mocked(api.get).mockClear();
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(box).toHaveValue("booth");
    expect(requested("/video-studio/projects?search=keynote&vis=mine&limit=25&offset=0")).toBe(false);
  });

  it("goes back to the last page when the one shown has emptied, never saying no video matches meanwhile", async () => {
    // The last page answers only when the test says so: the emptied page stays
    // on screen for the whole clamp, as on a slow network.
    let release: () => void = () => {};
    const lastPage = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.mocked(api.get).mockImplementation(((path: string) =>
      path === "/video-studio/projects?vis=mine&limit=25&offset=25"
        ? lastPage.then(() => answer(path))
        : Promise.resolve(answer(path))) as typeof api.get);
    open("/videos?page=4");

    await waitFor(() => expect(requested("/video-studio/projects?vis=mine&limit=25&offset=25")).toBe(true));
    expect(requested("/video-studio/projects?vis=mine&limit=25&offset=100")).toBe(true);
    expect(screen.queryByText("No videos match your filters.")).toBeNull();

    release();
    expect(await screen.findByText("Explainer 25")).toBeInTheDocument();
    expect(screen.queryByText("No videos match your filters.")).toBeNull();
  });

  it("says no video matches on the shared empty state card when a search finds none", async () => {
    vi.mocked(api.get).mockImplementation(((path: string) =>
      Promise.resolve(path.startsWith("/video-studio/projects?search=") ? { items: [], total: 0 } : answer(path))) as typeof api.get);
    open("/videos?q=nothing");

    // The table's own empty row: the search found nothing, the toolbar stays to change it.
    expect(await screen.findByText("No videos match your filters.")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search videos" })).toHaveValue("nothing");
  });
});

describe("The media library", () => {
  it("pages recordings and shots together and filters them by kind on the server", async () => {
    open("/videos/media");

    expect(await screen.findByText("A cow in a blue coat, shot 0")).toBeInTheDocument();
    // The shared table's server mode: a page of 50 (a size of its page size select), newest first.
    expect(requested("/video-studio/assets?limit=50&offset=0")).toBe(true);
    // The table's grid keeps the cards under its one toolbar and pager.
    expect(screen.getByTestId("data-table-grid")).toBeInTheDocument();
    expect(screen.getByTestId("data-table-toolbar")).toBeInTheDocument();
  });

  it("filters by kind on the server from the URL's `kind` key, the old links kept", async () => {
    open("/videos/media?kind=recordings");
    await waitFor(() => expect(requested("/video-studio/assets?kind=take&limit=50&offset=0")).toBe(true));
  });

  it("sorts both kinds together on the server and keeps the order in the URL", async () => {
    open("/videos/media?sort=title&dir=asc");
    await waitFor(() => expect(requested("/video-studio/assets?sort=title&direction=asc&limit=50&offset=0")).toBe(true));
  });

  it("finds the media no video uses on the server, and says when a video the reader cannot open uses one", async () => {
    open("/videos/media");
    expect(await screen.findByText("In a video you cannot open")).toBeInTheDocument();

    // The use filter sits behind the one "Filters" pill of the toolbar (#10215).
    expect(screen.queryByRole("button", { name: "In no video" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Filters" }));
    fireEvent.click(screen.getByRole("button", { name: "In no video" }));

    await waitFor(() => expect(requested("/video-studio/assets?usage=unused&limit=50&offset=0")).toBe(true));
    expect(screen.getByRole("button", { name: /Filters\s*1/ })).toBeInTheDocument();
  });

  it("toggles keep from a row's switch without opening the player, which the rest of the row opens (UX round 1 on #11198, B1)", async () => {
    vi.mocked(api.patch).mockReset();
    vi.mocked(api.patch).mockResolvedValue({});
    localStorage.setItem("copilot-view:media", "list");
    try {
      open("/videos/media");
      const rows = await screen.findAllByTestId("media-row");
      fireEvent.click(within(rows[1]).getByRole("switch", { name: "Keep its media" }));
      await waitFor(() => expect(api.patch).toHaveBeenCalled());
      expect(screen.queryByTestId("media.theater")).not.toBeInTheDocument();

      fireEvent.click(within(rows[1]).getByText("A cow in a blue coat, shot 1"));
      expect(await screen.findByTestId("media.theater")).toBeInTheDocument();
    } finally {
      localStorage.removeItem("copilot-view:media");
    }
  });

  it("draws one header row: the description is the title's info tip, not a paragraph (#10215)", async () => {
    open("/videos/media");
    expect(await screen.findByText("A cow in a blue coat, shot 0")).toBeInTheDocument();

    expect(screen.getByRole("img", { name: "About Video Studio" })).toBeInTheDocument();
    expect(screen.queryByText(/^Brand films, event loops/)).not.toBeInTheDocument();
  });

  it("asks the server for the media of a period", async () => {
    open("/videos/media?df=2026-10-01&dt=2026-10-03");
    expect(await screen.findByText("A cow in a blue coat, shot 0")).toBeInTheDocument();

    const from = encodeURIComponent(new Date(2026, 9, 1).toISOString());
    const to = encodeURIComponent(new Date(2026, 9, 3, 23, 59, 59, 999).toISOString());
    expect(requested(`/video-studio/assets?date_from=${from}&date_to=${to}&limit=50&offset=0`)).toBe(true);
    // The range is behind the Filters pill, counted in its badge (UX r1 B1 on #10312).
    expect(screen.getByRole("button", { name: /Filters\s*1/ })).toBeInTheDocument();
  });

  it("keeps one toolbar row: the time range is in the Filters panel and the retention rule is a tip (#10215)", async () => {
    open("/videos/media");
    expect(await screen.findByText("A cow in a blue coat, shot 0")).toBeInTheDocument();

    const toolbar = screen.getByTestId("data-table-toolbar");
    expect(within(toolbar).queryByRole("combobox", { name: "Time range" })).not.toBeInTheDocument();
    expect(screen.queryByText(/^Media expire after their retention/)).not.toBeInTheDocument();
    expect(within(toolbar).getByRole("img", { name: "About media retention" })).toBeInTheDocument();

    fireEvent.click(within(toolbar).getByRole("button", { name: "Filters" }));
    const panel = screen.getByRole("group", { name: "Filters" });
    expect(within(panel).getByRole("combobox", { name: "Time range" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "In no video" })).toBeInTheDocument();
  });

  it("says no media match in the table, its toolbar kept, when the filters find none", async () => {
    vi.mocked(api.get).mockImplementation(((path: string) =>
      Promise.resolve(path.startsWith("/video-studio/assets?kind=clip") ? { items: [], total: 0 } : answer(path))) as typeof api.get);
    open("/videos/media?kind=shots");

    expect(await screen.findByText("No media match your filters.")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search your recordings and AI shots" })).toBeInTheDocument();
  });

  it("deletes the media picked one by one, naming them", async () => {
    open("/videos/media");
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select A cow in a blue coat, shot 1" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select A cow in a blue coat, shot 2" }));

    const bar = screen.getByText("2 selected").closest("div") as HTMLElement;
    fireEvent.click(within(bar).getByRole("button", { name: "Delete" }));
    const said = await confirmDeletion();

    expect(said).toContain("Delete the 2 selected recordings and AI shots?");
    expect(said).toContain("No video uses them. Their files are deleted for good.");
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/video-studio/assets/bulk-delete", {
        items: [
          { kind: "clip", id: "c-1" },
          { kind: "clip", id: "c-2" },
        ],
      }),
    );
  });

  it("keeps the picked media a video uses, says so before and after, and leaves forcing to the card", async () => {
    vi.mocked(api.post).mockResolvedValue({ deleted: 1, kept_in_use: 1 });
    open("/videos/media");
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select A cow in a blue coat, shot 0" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Select A cow in a blue coat, shot 1" }));

    const bar = screen.getByText("2 selected").closest("div") as HTMLElement;
    fireEvent.click(within(bar).getByRole("button", { name: "Delete" }));
    const said = await confirmDeletion();

    expect(said).toContain("1 of them is in a video and is kept: delete those one at a time from their card.");
    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("1 kept: a video uses it.")).toBeInTheDocument();
  });

  it("selects every medium no video uses in one click and deletes them by the filters, read again on the server", async () => {
    vi.mocked(api.post).mockResolvedValue({ deleted: SHOTS, kept_in_use: 0 });
    open("/videos/media?kind=shots");
    expect(await screen.findByText("A cow in a blue coat, shot 0")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("media.selectUnused"));

    await waitFor(() => expect(requestedLike(/^\/video-studio\/assets\?kind=clip&usage=unused&/)).toBe(true));
    // The table's selection bar counts every match, past the page.
    const bar = (await screen.findByText(`${SHOTS} selected`)).closest("div") as HTMLElement;
    expect(screen.getByRole("checkbox", { name: "Deselect A cow in a blue coat, shot 47" })).toBeInTheDocument();
    fireEvent.click(within(bar).getByRole("button", { name: "Delete" }));
    expect(await confirmDeletion()).toContain(`Delete the ${SHOTS} recordings and AI shots matching your filters?`);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith("/video-studio/assets/bulk-delete", {
        all_matching_filters: true,
        kind: "clip",
        usage: "unused",
      }),
    );
    expect(await screen.findByText(`${SHOTS} recordings and AI shots deleted`)).toBeInTheDocument();
  });
});

describe("Deleting a medium", () => {
  const shot = (id: string, prompt: string, usedIn: { id: string; title: string }[]) => ({
    kind: "clip",
    id,
    name: null,
    prompt,
    status: "ready",
    keep: false,
    expired_at: null,
    expires_at: null,
    used_in: usedIn,
  });

  beforeEach(() => {
    vi.mocked(api.delete).mockReset();
    vi.mocked(api.get).mockImplementation(((path: string) =>
      Promise.resolve(
        path.startsWith("/video-studio/assets?")
          ? { items: [shot("c-used", "A shot in a film", [{ id: "v-1", title: "My film" }]), shot("c-free", "A spare shot", [])], total: 2 }
          : answer(path),
      )) as typeof api.get);
  });

  const typeTheCode = (dialog: HTMLElement) => {
    const code = within(dialog).getByTestId("confirm-challenge-code").textContent ?? "";
    fireEvent.change(within(dialog).getByPlaceholderText("Type the code to enable the button"), { target: { value: code } });
  };

  it("names the videos a used one plays in and deletes it only once the code is typed back", async () => {
    vi.mocked(api.delete).mockResolvedValue(undefined as never);
    open("/videos/media");
    await screen.findByText("A shot in a film");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete an AI shot that videos use?")).toBeInTheDocument();
    expect(within(dialog).getByText(/It is used in 1 video: My film\./)).toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", { name: "Delete anyway" });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(api.delete).not.toHaveBeenCalled();

    typeTheCode(dialog);
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/video-studio/clips/c-used?force=true"));
  });

  it("asks again with the code when the server says a video the page did not know of uses it", async () => {
    vi.mocked(api.delete)
      .mockRejectedValueOnce(
        Object.assign(new Error("used"), {
          status: 409,
          detail: { code: "media_in_use", message: "used", videos: [], others: 1 },
        }),
      )
      .mockResolvedValueOnce(undefined as never);
    open("/videos/media");
    await screen.findByText("A spare shot");

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[1]);
    const first = await screen.findByRole("dialog");
    expect(within(first).getByText("Delete this AI shot?")).toBeInTheDocument();
    fireEvent.click(within(first).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/video-studio/clips/c-free"));

    const second = await screen.findByText("Delete an AI shot that videos use?");
    const dialog = second.closest("[role=dialog]") as HTMLElement;
    expect(within(dialog).getByText(/It is used in 1 video you cannot open\./)).toBeInTheDocument();
    typeTheCode(dialog);
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete anyway" }));

    await waitFor(() => expect(api.delete).toHaveBeenLastCalledWith("/video-studio/clips/c-free?force=true"));
  });
});
