// The relative times of the agent account pages read in the viewer's language
// every one goes through the
// shared `formatTimeAgo` / `relativeTimeParts` of lib/relative-time.ts, never the
// English `timeAgo()` of lib/utils.ts, and no page keeps a copy of its own.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { IntlProvider } from "react-intl";
import { MemoryRouter } from "react-router-dom";

import { ToastProvider } from "@/components/ui/toast";
import fr from "@/i18n/lang/fr.json";
import ja from "@/i18n/lang/ja.json";
import type { AgentAccountReply } from "@/types";
import { AgentAccountReplyCard } from "./AgentAccountReplyCard";

vi.mock("@/hooks/api/useAgentAccountApi", () => ({
  useAgentAccountProvider: () => undefined,
  useApproveAgentAccountReply: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRejectAgentAccountReply: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const minutesAgo = (n: number) => new Date(Date.now() - n * 60 * 1000).toISOString();

const REPLY: AgentAccountReply = {
  id: "r1", account_id: "acct", account_name: "My digital twin", provider: "microsoft_teams", status: "sent",
  conversation_type: "direct", conversation_name: null, sender_name: "Alice", sender_email: null,
  inbound_text: "Can you share the board deck?", draft_text: "Here it is.", sent_text: "Here it is.",
  rule_name: null, ai_label: true, error: null, decided_at: minutesAgo(5), created_at: minutesAgo(9),
  rating_positive: 0, rating_negative: 0, kind: "reply",
};

function renderIn(locale: string, messages: Record<string, string>) {
  return render(
    <IntlProvider locale={locale} messages={messages} onError={() => {}}>
      <ToastProvider>
        <MemoryRouter>
          <AgentAccountReplyCard reply={REPLY} />
        </MemoryRouter>
      </ToastProvider>
    </IntlProvider>,
  );
}

describe("agent account relative times", () => {
  it("reads the reply card's times in French", () => {
    renderIn("fr", fr);
    expect(screen.getByText("il y a 9 minutes")).toBeTruthy();
    expect(screen.getByText("Approuvée il y a 5 minutes")).toBeTruthy();
    expect(screen.queryByText(/ago/)).toBeNull();
  });

  it("reads the reply card's times in Japanese, with no space before the particle", () => {
    renderIn("ja", ja);
    expect(screen.getByText("9 分前")).toBeTruthy();
    expect(screen.getByText("5 分前に承認")).toBeTruthy();
    expect(ja["Checked {when}"]).toBe("{when}に確認");
  });

  it("keeps the English timeAgo() and a page-local copy of the helper out of every page", () => {
    // vitest runs from frontend/ (CI and `yarn test`). Widened from the agent
    // account pages to the whole of src/pages: a page reads its relative times
    // through `formatTimeAgo` / `formatTimeAgoLine` of lib/relative-time.ts.
    const sources = (readdirSync(join("src", "pages"), { recursive: true }) as string[])
      .filter((name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name))
      .map((name) => join("src", "pages", name));
    expect(sources.length).toBeGreaterThan(100);
    // Comment lines name the old helper on purpose; only code counts.
    const code = (path: string) =>
      readFileSync(path, "utf8").split("\n").filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line)).join("\n");
    const offenders = sources.filter((path) => {
      const text = code(path);
      return /(?<![\w.])timeAgo\s*\(/.test(text) || /from\s+["']@\/pages\/settings\/security-audit["']/.test(text);
    });
    expect(offenders).toEqual([]);
  });

  it("leaves no page special-casing the 0-second bucket of relativeTimeParts by hand", () => {
    const sources = (readdirSync(join("src", "pages"), { recursive: true }) as string[])
      .filter((name) => /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name))
      .map((name) => join("src", "pages", name));
    const offenders = sources.filter((path) => /\bparts\.value\s*===\s*0\b/.test(readFileSync(path, "utf8")));
    expect(offenders).toEqual([]);
  });
});
