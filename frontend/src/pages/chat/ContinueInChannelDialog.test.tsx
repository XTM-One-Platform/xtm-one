// Continue a conversation in Slack or Teams, or stop: the dialog.

import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";

import { api } from "@/lib/api";
import { renderWithProviders } from "@/testing/providers";
import type {
  ContinuationChannelState,
  ContinuationUnavailableReason,
  ConversationChannelBindings,
} from "@/types";
import { ContinueInChannelDialog } from "./ContinueInChannelDialog";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

const platform = vi.hoisted(() => ({ aiEnabled: true }));
vi.mock("@/hooks/usePlatformConfig", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/usePlatformConfig")>()),
  useAiEnabled: () => platform.aiEnabled,
}));

const get = vi.mocked(api.get);
const post = vi.mocked(api.post);
const remove = vi.mocked(api.delete);

const URL = "/chat/conversations/c-1/channel-bindings";
const BINDING = { created_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-01T10:00:00Z" };

function available(channel: "slack" | "teams"): ContinuationChannelState {
  return { channel, available: true, reason: null, detail: null };
}

function unavailable(
  channel: "slack" | "teams",
  reason: string,
  detail: string | null = null,
): ContinuationChannelState {
  return { channel, available: false, reason: reason as ContinuationUnavailableReason, detail };
}

function serve(...states: ConversationChannelBindings[]) {
  for (const state of states) get.mockResolvedValueOnce(state);
  get.mockResolvedValue(states[states.length - 1]);
}

function openDialog() {
  return renderWithProviders(<ContinueInChannelDialog conversationId="c-1" open onClose={() => {}} />);
}

/** The row of *label* in the dialog. */
async function row(label: string): Promise<HTMLElement> {
  const name = await screen.findByText(label, { selector: "p" });
  return name.closest("div.flex") as HTMLElement;
}

beforeEach(() => {
  platform.aiEnabled = true;
  get.mockReset();
  post.mockReset();
  remove.mockReset();
});

describe("ContinueInChannelDialog", () => {
  it("offers a channel that can take the conversation and says why another cannot", async () => {
    serve({ bindings: [], channels: [available("slack"), unavailable("teams", "teams_chat_unknown")] });
    openDialog();

    const slack = await row("Slack");
    expect(within(slack).getByText("Not continued here yet.")).toBeTruthy();
    expect(within(slack).getByRole("button", { name: "Continue in Slack" }).hasAttribute("disabled")).toBe(false);
    const teams = await row("Teams");
    expect(within(teams).getByText("Send a message to the bot in Microsoft Teams once, then try again.")).toBeTruthy();
    expect(within(teams).getByRole("button", { name: "Continue in Teams" }).hasAttribute("disabled")).toBe(true);
    expect(get).toHaveBeenCalledWith(URL);
  });

  it.each([
    ["not_continuable", "This conversation is a group thread: continue it where it started."],
    ["not_configured", "No Slack bot is connected. An administrator connects one in Settings > Integrations > Channels & Bots."],
    [
      "direct_messages_off",
      "The Slack bot does not answer direct messages. An administrator changes it in Settings > Integrations > Channels & Bots.",
    ],
    ["agent_unavailable", "The agent of this conversation does not answer on Slack."],
    ["started_there", "This conversation is your chat with the bot in Slack: continue it there."],
    // A reason the web app does not word: the server's own sentence.
    ["delivery_failed", "Slack could not be reached. Try again in a moment."],
  ])("words the %s reason", async (reason, sentence) => {
    serve({
      bindings: [],
      channels: [unavailable("slack", reason, "Slack could not be reached. Try again in a moment."), available("teams")],
    });
    openDialog();

    const slack = await row("Slack");
    expect(within(slack).getByText(sentence)).toBeTruthy();
    expect(within(slack).getByRole("button", { name: "Continue in Slack" }).hasAttribute("disabled")).toBe(true);
  });

  it("continues in a channel, says so, and shows it continued there", async () => {
    serve(
      { bindings: [], channels: [available("slack"), available("teams")] },
      { bindings: [{ channel: "slack", ...BINDING }], channels: [available("slack"), available("teams")] },
    );
    post.mockResolvedValue({ channel: "slack", ...BINDING });
    openDialog();

    fireEvent.click(within(await row("Slack")).getByRole("button", { name: "Continue in Slack" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith(URL, { channel: "slack" }));
    expect(await screen.findByText("Continued in Slack")).toBeTruthy();
    expect(screen.getByText("The bot sent you a direct message. Reply in its thread to continue.")).toBeTruthy();
    // The bindings are read again: the row now offers to stop.
    const slack = await row("Slack");
    await waitFor(() =>
      expect(within(slack).getByText("Continued in your direct messages with the bot.")).toBeTruthy(),
    );
    expect(within(slack).getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(within(await row("Teams")).getByRole("button", { name: "Continue in Teams" })).toBeTruthy();
  });

  it("says where to reply in Teams", async () => {
    serve({ bindings: [], channels: [unavailable("slack", "not_configured"), available("teams")] });
    post.mockResolvedValue({ channel: "teams", ...BINDING });
    openDialog();

    fireEvent.click(within(await row("Teams")).getByRole("button", { name: "Continue in Teams" }));

    await waitFor(() => expect(post).toHaveBeenCalledWith(URL, { channel: "teams" }));
    expect(await screen.findByText("Continued in Teams")).toBeTruthy();
    expect(screen.getByText("The bot wrote in your chat with it. Reply there to continue.")).toBeTruthy();
  });

  it("shows a refusal in place, without a success toast", async () => {
    serve({ bindings: [], channels: [available("slack"), available("teams")] });
    post.mockRejectedValue(new Error("Slack did not accept the message."));
    openDialog();

    fireEvent.click(within(await row("Slack")).getByRole("button", { name: "Continue in Slack" }));

    expect(await screen.findByText("Slack did not accept the message.")).toBeTruthy();
    expect(screen.queryByText("Continued in Slack")).toBeNull();
    expect(within(await row("Slack")).getByText("Not continued here yet.")).toBeTruthy();
  });

  it("stops continuing a conversation in a channel", async () => {
    serve(
      { bindings: [{ channel: "teams", ...BINDING }], channels: [available("slack"), available("teams")] },
      { bindings: [], channels: [available("slack"), available("teams")] },
    );
    remove.mockResolvedValue(undefined);
    openDialog();

    fireEvent.click(within(await row("Teams")).getByRole("button", { name: "Stop" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(`${URL}/teams`));
    const teams = await row("Teams");
    await waitFor(() =>
      expect(within(teams).getByRole("button", { name: "Continue in Teams" })).toBeTruthy(),
    );
  });

  it("keeps a stop refused by the server on screen", async () => {
    serve({ bindings: [{ channel: "slack", ...BINDING }], channels: [available("slack"), available("teams")] });
    remove.mockRejectedValue(new Error("Conversation not found"));
    openDialog();

    fireEvent.click(within(await row("Slack")).getByRole("button", { name: "Stop" }));

    expect(await screen.findByText("Conversation not found")).toBeTruthy();
  });

  it("offers no continuation while AI is disabled, but still lets the user stop one", async () => {
    platform.aiEnabled = false;
    serve({ bindings: [{ channel: "teams", ...BINDING }], channels: [available("slack"), available("teams")] });
    openDialog();

    expect(within(await row("Slack")).getByRole("button", { name: "Continue in Slack" }).hasAttribute("disabled")).toBe(
      true,
    );
    expect(within(await row("Teams")).getByRole("button", { name: "Stop" }).hasAttribute("disabled")).toBe(false);
  });

  it("disables every button while a request is on its way", async () => {
    serve({ bindings: [], channels: [available("slack"), available("teams")] });
    let answer: (value: unknown) => void = () => {};
    post.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    openDialog();

    fireEvent.click(within(await row("Slack")).getByRole("button", { name: "Continue in Slack" }));

    const teams = within(await row("Teams")).getByRole("button", { name: "Continue in Teams" });
    await waitFor(() => expect(teams.hasAttribute("disabled")).toBe(true));
    answer({ channel: "slack", ...BINDING });
    await waitFor(() => expect(teams.hasAttribute("disabled")).toBe(false));
  });

  it("says why the channels could not be read", async () => {
    get.mockRejectedValue(new Error("Conversation not found"));
    openDialog();

    expect(await screen.findByText("Conversation not found")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Continue in/ })).toBeNull();
  });

  it("keeps the buttons disabled until the bindings are read again, so a second click sends nothing", async () => {
    get.mockResolvedValueOnce({ bindings: [], channels: [available("slack"), available("teams")] });
    let refreshed: (value: ConversationChannelBindings) => void = () => {};
    get.mockReturnValueOnce(new Promise((resolve) => (refreshed = resolve)));
    post.mockResolvedValue({ channel: "slack", ...BINDING });
    openDialog();

    const continueSlack = within(await row("Slack")).getByRole("button", { name: "Continue in Slack" });
    fireEvent.click(continueSlack);
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2)); // bound, and read again
    expect(continueSlack.hasAttribute("disabled")).toBe(true);
    fireEvent.click(continueSlack);
    expect(post).toHaveBeenCalledTimes(1);

    refreshed({ bindings: [{ channel: "slack", ...BINDING }], channels: [available("slack"), available("teams")] });
    expect(await within(await row("Slack")).findByRole("button", { name: "Stop" })).toBeTruthy();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("reads nothing while it is closed", () => {
    renderWithProviders(<ContinueInChannelDialog conversationId="c-1" open={false} onClose={() => {}} />);
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByText("Continue in Slack or Teams")).toBeNull();
  });
});
