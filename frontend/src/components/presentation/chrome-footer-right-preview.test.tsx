/**
 * The template design editor previews the right footer pattern:
 * "Preview with sample values" lists the right footer
 * filled with the sample values, the slide number and the year, next to the
 * footer and the header, and only when the template declares one.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";

import { TemplateDesignEditor } from "./TemplateDesignEditor";

function renderEditor(chrome: Record<string, unknown>) {
  return renderWithProviders(
    <TemplateDesignEditor
      themeConfig={{}}
      extraMetadata={{ chrome }}
      onThemeConfigChange={() => {}}
      onExtraMetadataChange={() => {}}
    />,
  );
}

describe("TemplateDesignEditor right footer preview", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-03T08:00:00Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("fills the right footer pattern with the sample page, year and variables", () => {
    renderEditor({
      footer: "{{title}}",
      footer_right: "{{page}} \u00b7 \u00a9 {{year}} {{client}}",
      variables: [{ key: "client", label: "Client", default: "Acme" }],
    });
    const line = screen.getByTestId("template-chrome-preview-footer-right");
    expect(line.textContent).toBe("Right footer: 01 \u00b7 \u00a9 2026 Acme");
  });

  it("previews a right footer declared without a footer or a header", () => {
    renderEditor({ footer_right: "\u00a9 {{year}}" });
    expect(screen.getByText("Preview with sample values")).toBeTruthy();
    expect(screen.getByTestId("template-chrome-preview-footer-right").textContent).toBe("Right footer: \u00a9 2026");
  });

  it("shows no right footer line when the template declares none", () => {
    renderEditor({ footer: "{{title}}" });
    expect(screen.getByText("Preview with sample values")).toBeTruthy();
    expect(screen.queryByTestId("template-chrome-preview-footer-right")).toBeNull();
  });
});
