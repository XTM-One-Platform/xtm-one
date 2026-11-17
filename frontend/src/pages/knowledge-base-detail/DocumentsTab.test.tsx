// A knowledge base document's "source" link: shown only when the page it was
// ingested from is a web address (`lib/safe-url.ts`), never followed otherwise.

import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { KnowledgeBase, KnowledgeDocument } from "@/types";
import { DocumentsTab } from "./DocumentsTab";

let docs: KnowledgeDocument[] = [];
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useKnowledgeBaseApi", () => ({
  useKnowledgeDocuments: () => ({ data: docs }),
  useKnowledgeDocumentCount: () => ({ data: { count: docs.length } }),
  useAddTextDocument: () => idle,
  useUploadDocument: () => idle,
  useDeleteDocument: () => idle,
  useReprocessDocuments: () => idle,
  downloadDocument: vi.fn(),
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "user-1" }, isAdmin: false }) }));
vi.mock("@/hooks/useAiDisabled", () => ({ useAiActionDisabled: () => false }));

function renderDocument(sourceUrl: string) {
  docs = [
    {
      id: "doc-1",
      filename: "advisory.html",
      status: "ready",
      chunk_count: 3,
      source_type: "url",
      source_url: sourceUrl,
      created_at: "2026-10-01T00:00:00Z",
    } as unknown as KnowledgeDocument,
  ];
  const kb = { id: "kb-1", user_id: "user-1", document_count: 1 } as unknown as KnowledgeBase;
  return renderWithProviders(<DocumentsTab kb={kb} canManage={false} />);
}

const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("DocumentsTab source link", () => {
  it("links a web address", () => {
    const { container } = renderDocument("https://vendor.example.com/advisories/42");
    expect(hrefs(container)).toEqual(["https://vendor.example.com/advisories/42"]);
  });

  it.each(HOSTILE_URLS)("never links %s", (_label, url) => {
    const { container } = renderDocument(url);
    expect(hrefs(container)).toEqual([]);
    expect(container.textContent).toContain("advisory.html");
  });
});
