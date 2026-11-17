// The API Spec tab's Source: the address a spec was imported from is a link
// only when it is a web address (`lib/safe-url.ts`), else plain text.

import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/testing/providers";
import { HOSTILE_URLS } from "@/testing/hostile-urls";
import type { Integration, OpenAPISpec } from "@/types";
import { OpenAPISpecTab } from "./OpenAPISpecTab";

let spec: OpenAPISpec;
const idle = { mutate: vi.fn(), mutateAsync: vi.fn(), isPending: false };
vi.mock("@/hooks/api/useIntegrationApi", () => ({
  useIntegrationOpenAPISpec: () => ({ data: spec, isLoading: false, error: null }),
  useUpdateOpenAPIOperations: () => idle,
  useRefreshOpenAPISpec: () => idle,
  useReplaceOpenAPISpec: () => idle,
  useSetClientCertificate: () => idle,
  useDeleteClientCertificate: () => idle,
  useUpdateIntegration: () => idle,
}));
vi.mock("@/hooks/useAuth", () => ({ useAuth: () => ({ user: { id: "user-1" }, isAdmin: false }) }));

function renderSpec(sourceUrl: string) {
  spec = {
    integration_id: "integration-1",
    source_type: "url",
    source_url: sourceUrl,
    spec_version: "3.0.3",
    document_format: "json",
    title: "Petstore",
    api_version: "1.0.0",
    tool_prefix: "petstore",
    auth_options: [],
    tags: [],
    operations: [],
    selected_count: 0,
    supported_count: 0,
    warnings: [],
  };
  const integration = { id: "integration-1", auth_type: "none", user_id: "someone-else", is_builtin: false } as unknown as Integration;
  return renderWithProviders(<OpenAPISpecTab integration={integration} />);
}

const hrefs = (root: HTMLElement) => [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));

describe("OpenAPISpecTab source", () => {
  it("links a web address", () => {
    const { container } = renderSpec("https://petstore.example.com/openapi.json");
    expect(hrefs(container)).toEqual(["https://petstore.example.com/openapi.json"]);
  });

  it.each(HOSTILE_URLS)("shows %s as text, never as a link", (_label, url) => {
    const { container } = renderSpec(url);
    expect(hrefs(container)).toEqual([]);
    expect(container.textContent).toContain(url);
  });
});
