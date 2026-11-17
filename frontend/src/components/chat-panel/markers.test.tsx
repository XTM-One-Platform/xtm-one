// The cards of an answer's markers on the chat panel: each card reading the
// id the panel split off its marker, the gated ones behind their flag, a streamed chart
// marker drawing nothing, a card that fails saying so in its own place, and a
// file the conversation has not stored yet downloading through the platform's
// own route.

import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";
import { renderHook, screen } from "@testing-library/react";
import type { ChatMarkerRenderer, ChatMessage as PanelMessage } from "@filigran/chatbot";

import {
  agentSwitchMarker,
  useAgentSwitchRenderer,
  useMarkerRenderers,
  useTranscriptCards,
} from "@/components/chat-panel/markers";
import { XTM_HOST_MARKER_KINDS } from "@/pages/chat/answerMarkers";
import { renderWithProviders, testQueryClient, withProviders } from "@/testing/providers";
import type { ChatMessage, ChatMessageAttachment } from "@/types";

vi.mock("@/components/chat-panel/FileChips", () => ({
  FileChip: ({ att }: { att: ChatMessageAttachment }) => (
    <span data-testid="file-chip" data-url={att.download_url ?? ""}>
      {att.filename}
    </span>
  ),
  WorkingFileChip: ({ att }: { att: ChatMessageAttachment }) => <span data-testid="working-chip">{att.filename}</span>,
}));
vi.mock("@/components/chat-panel/MarkerInlineChip", () => ({
  MarkerInlineChip: ({ kind, id, slideRef }: { kind: string; id: string; slideRef?: string | null }) => (
    <span data-testid="inline-chip">{`${kind}:${id}#${slideRef ?? ""}`}</span>
  ),
}));
vi.mock("@/components/presentation/PresentationCard", () => ({
  PresentationCard: ({ presentationId, focusSlideRef }: { presentationId: string; focusSlideRef?: string | null }) => (
    <span data-testid="deck">{`${presentationId}#${focusSlideRef ?? ""}`}</span>
  ),
}));
vi.mock("@/components/report/ReportCard", () => ({
  ReportCard: ({ reportId }: { reportId: string }) => {
    if (reportId === BROKEN) throw new Error("the card failed");
    return <span data-testid="report">{reportId}</span>;
  },
}));
vi.mock("@/components/video-studio/VideoCard", () => ({
  VideoCard: ({ videoId }: { videoId: string }) => <span data-testid="video">{videoId}</span>,
}));
vi.mock("@/components/custom-dashboard/CustomDashboardCard", () => ({
  CustomDashboardCard: ({ dashboardId }: { dashboardId: string }) => <span data-testid="dashboard">{dashboardId}</span>,
}));
vi.mock("@/components/investigation/InvestigationBlock", () => ({
  InvestigationBlock: ({ investigationId }: { investigationId: string }) => (
    <span data-testid="investigation">{investigationId}</span>
  ),
}));
vi.mock("@/components/work-items/ScheduledWorkItemCard", () => ({
  ScheduledWorkItemCard: ({ workItemId }: { workItemId: string }) => <span data-testid="work-item">{workItemId}</span>,
}));

const ID = "11111111-2222-4333-8444-555555555555";
const BROKEN = "99999999-2222-4333-8444-555555555555";
const FILE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";

const panelMessage = (overrides: Partial<PanelMessage> = {}): PanelMessage =>
  ({ id: "m-1", role: "assistant", content: "", timestamp: new Date(), ...overrides }) as PanelMessage;

const storedReply = (attachments: ChatMessageAttachment[]): ChatMessage =>
  ({ id: "s-1", role: "assistant", content: "", created_at: "2026-10-22T10:00:00Z", attachments }) as ChatMessage;

function renderers(
  stored: ChatMessage[] | undefined,
  flags = { deepInvestigation: true, videoStudio: true, dashboardStudio: true },
): Record<string, ChatMarkerRenderer> {
  const { result } = renderHook(() => useMarkerRenderers(stored, flags), { wrapper: withProviders(testQueryClient()) });
  return result.current;
}

function draw(renderer: ChatMarkerRenderer, args: string, message = panelMessage()) {
  const node = renderer.render(args, { message, streaming: false });
  if (node === null) return null;
  return renderWithProviders(node as ReactElement);
}

describe("useMarkerRenderers", () => {
  it("draws a stored file as its chip, a working file as the compact one, and nothing for an unknown file", () => {
    const att = (tag: "download_file" | "working_file", id = FILE_ID): ChatMessageAttachment => ({
      file_id: id,
      filename: `${tag}.csv`,
      type: "CSV",
      size: 10,
      download_url: `/api/v1/chat/files/${id}/download`,
      file_tag: tag,
    });
    const other = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee";
    const { FILE } = renderers([storedReply([att("download_file"), att("working_file", other)])]);
    draw(FILE, FILE_ID.toUpperCase());
    expect(screen.getByTestId("file-chip")).toHaveTextContent("download_file.csv");
    draw(FILE, other);
    expect(screen.getByTestId("working-chip")).toHaveTextContent("working_file.csv");
    expect(FILE.render("cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee", { message: panelMessage(), streaming: false })).toBeNull();
  });

  it("draws a file marker inside a table cell as the compact chip, never the card (#11697)", () => {
    const { FILE } = renderers([
      storedReply([
        {
          file_id: FILE_ID,
          filename: "valuation.pdf",
          type: "PDF",
          size: 10,
          download_url: `/api/v1/chat/files/${FILE_ID}/download`,
          file_tag: "download_file",
        },
      ]),
    ]);
    const node = FILE.render(FILE_ID, { message: panelMessage(), streaming: false, inline: true } as Parameters<
      ChatMarkerRenderer["render"]
    >[1]);
    renderWithProviders(node as ReactElement);
    expect(screen.getByTestId("working-chip")).toHaveTextContent("valuation.pdf");
    expect(screen.queryByTestId("file-chip")).toBeNull();
  });

  it.each([
    ["PRESENTATION", ID, `PRESENTATION:${ID}#`],
    ["PRESENTATION", `${ID}#/s-3`, `PRESENTATION:${ID}#s-3`],
    ["REPORT", ID, `REPORT:${ID}#`],
    ["VIDEO", ID, `VIDEO:${ID}#`],
    ["DASHBOARD", ID, `DASHBOARD:${ID}#`],
    ["INVESTIGATION", ID, `INVESTIGATION:${ID}#`],
    ["WORK_ITEM", ID, `WORK_ITEM:${ID}#`],
  ])("draws a %s marker inside a table cell or a list as its compact chip, never the card (#11697)", (kind, args, chip) => {
    const renderer = renderers([])[kind];
    const node = renderer.render(args, { message: panelMessage(), streaming: false, inline: true } as Parameters<
      ChatMarkerRenderer["render"]
    >[1]);
    renderWithProviders(node as ReactElement);
    expect(screen.getByTestId("inline-chip")).toHaveTextContent(chip);
    for (const card of ["deck", "report", "video", "dashboard", "investigation", "work-item"]) {
      expect(screen.queryByTestId(card)).toBeNull();
    }
  });

  it("draws no inline chip for a kind whose feature is off, or for a chart", () => {
    const off = renderers([], { deepInvestigation: false, videoStudio: false, dashboardStudio: false });
    const inline = { message: panelMessage(), streaming: false, inline: true } as Parameters<ChatMarkerRenderer["render"]>[1];
    expect(off.VIDEO.render(ID, inline)).toBeNull();
    expect(off.DASHBOARD.render(ID, inline)).toBeNull();
    expect(off.INVESTIGATION.render(ID, inline)).toBeNull();
    expect(off.CHART.render(ID, inline)).toBeNull();
  });

  it("downloads a file of a reply that has just landed through the chat's own file route, root-relative", () => {
    const { FILE } = renderers(undefined);
    const message = panelMessage({
      attachments: [{ fileId: FILE_ID, filename: "report.pdf", contentType: "application/pdf", size: 42 }],
    });
    draw(FILE, FILE_ID, message);
    expect(screen.getByTestId("file-chip")).toHaveAttribute("data-url", `/api/v1/chat/files/${FILE_ID}/download`);
  });

  it("draws XTM One's own marker kinds, the ones the pages outside the panel read answers with", () => {
    const builtIn = ["FILE", "PRESENTATION", "REPORT", "VIDEO"];
    const own = Object.keys(renderers(undefined)).filter((name) => !builtIn.includes(name));
    expect(own.sort()).toEqual(XTM_HOST_MARKER_KINDS);
  });

  it("reads each card's id from its marker's arguments: a deck with its slide, a report, a work item", () => {
    const r = renderers(undefined);
    draw(r.PRESENTATION, `${ID}#/slide-2`);
    expect(screen.getByTestId("deck")).toHaveTextContent(`${ID}#slide-2`);
    draw(r.REPORT, ID.toUpperCase());
    expect(screen.getByTestId("report")).toHaveTextContent(ID);
    draw(r.WORK_ITEM, ID);
    expect(screen.getByTestId("work-item")).toHaveTextContent(ID);
    expect(r.PRESENTATION.fullWidth).toBe(true);
    // Every card kind draws at the transcript's full width, the work item card included (#10910).
    for (const kind of ["REPORT", "DASHBOARD", "INVESTIGATION", "WORK_ITEM"]) {
      expect(r[kind].fullWidth).toBe(true);
    }
  });

  it("draws nothing for a marker that names no id", () => {
    const r = renderers(undefined);
    const context = { message: panelMessage(), streaming: false };
    expect(r.PRESENTATION.render("not-an-id", context)).toBeNull();
    expect(r.PRESENTATION.render("not-an-id#/slide-2", context)).toBeNull();
    for (const kind of ["REPORT", "VIDEO", "DASHBOARD", "INVESTIGATION", "WORK_ITEM"]) {
      expect(r[kind].render(`${ID} trailing`, context)).toBeNull();
    }
  });

  it("opens a deck on no slide when its marker names none", () => {
    const r = renderers(undefined);
    draw(r.PRESENTATION, ID);
    expect(screen.getByTestId("deck")).toHaveTextContent(`${ID}#`);
  });

  it("draws a video, a dashboard and an investigation only behind their flags", () => {
    const on = renderers(undefined);
    draw(on.VIDEO, ID);
    expect(screen.getByTestId("video")).toHaveTextContent(ID);
    draw(on.DASHBOARD, ID);
    expect(screen.getByTestId("dashboard")).toHaveTextContent(ID);
    expect(on.DASHBOARD.fullWidth).toBe(true);
    draw(on.INVESTIGATION, ID);
    expect(screen.getByTestId("investigation")).toHaveTextContent(ID);

    const off = renderers(undefined, { deepInvestigation: false, videoStudio: false, dashboardStudio: false });
    const context = { message: panelMessage(), streaming: false };
    expect(off.VIDEO.render(ID, context)).toBeNull();
    expect(off.DASHBOARD.render(ID, context)).toBeNull();
    expect(off.INVESTIGATION.render(ID, context)).toBeNull();
  });

  it("hides a streamed chart marker: the stored answer carries the chart itself", () => {
    const { CHART } = renderers(undefined);
    expect(CHART.render("0123456789ab", { message: panelMessage(), streaming: true })).toBeNull();
    expect(CHART.render("0123456789ab", { message: panelMessage(), streaming: false })).toBeNull();
  });

  it("says a card failed in its own place", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const { REPORT } = renderers(undefined);
    draw(REPORT, BROKEN);
    expect(screen.getByText("This item could not be loaded.")).toBeInTheDocument();
  });
});

describe("useTranscriptCards", () => {
  it("moves the newest card of a deck to a slide, and none once its cards are gone", () => {
    const { result } = renderHook(() => useTranscriptCards("c-1", undefined), {
      wrapper: withProviders(testQueryClient()),
    });
    const older = vi.fn(() => true);
    const newer = vi.fn(() => true);
    const { registerSlideStage } = result.current.cardContext;
    const dropOlder = registerSlideStage(ID, older);
    const dropNewer = registerSlideStage(ID, newer);
    expect(result.current.navigateToSlide(ID, "s-2")).toBe(true);
    expect(newer).toHaveBeenCalledWith("s-2");
    expect(older).not.toHaveBeenCalled();
    dropNewer();
    dropOlder();
    expect(result.current.navigateToSlide(ID, "s-2")).toBe(false);
    expect(result.current.cardContext.conversationId).toBe("c-1");
  });
});

describe("the change-of-agent note", () => {
  const note = {
    id: "s-switch",
    role: "assistant",
    content: "Continued by Deck Bot on 2026-10-29.",
    created_at: "2026-10-29T10:00:00Z",
    agent_switch: {
      from_agent: { agent_id: "a-video", agent_name: "Video Bot", general_assistant: false, deleted: false },
      to_agent: { agent_id: "a-deck", agent_name: "Deck Bot", general_assistant: false, deleted: false },
      reason: "continued",
    },
  } as ChatMessage;

  it("reads as its marker, any other message as its own text", () => {
    expect(agentSwitchMarker(note)).toBe("[[AGENT_SWITCH:s-switch]]");
    expect(agentSwitchMarker(storedReply([]))).toBeNull();
    expect(agentSwitchMarker(undefined)).toBeNull();
  });

  it("draws its marker as the divider across the thread, and nothing for a message that is no note", () => {
    const { result } = renderHook(() => useAgentSwitchRenderer([note, storedReply([])]));
    const renderer = result.current.AGENT_SWITCH;
    expect(renderer.fullWidth).toBe(true);
    draw(renderer, "s-switch");
    expect(screen.getByRole("note").textContent).toMatch(/^Continued by Deck Bot on /);
    expect(renderer.render("s-1", { message: panelMessage(), streaming: false })).toBeNull();
  });
});
