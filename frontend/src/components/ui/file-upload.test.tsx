import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "@/testing/providers";
import { FileUpload } from "./file-upload";

// The uploader is a form field like any other (UX r1 B3 on #9949): its label
// names the drop zone and the file input, its helper describes them, and the
// filled state keeps the default control size.
describe("FileUpload", () => {
  it("names the drop zone and the file input by its label and describes them by its helper", () => {
    const { container } = renderWithProviders(
      <FileUpload name="platform_logo" label="Logo" help="Displayed in the sidebar." value={null} maxBytes={64 * 1024} onChange={vi.fn()} />,
    );
    const zone = screen.getByRole("button", { name: "Logo" });
    expect(zone.getAttribute("aria-describedby") ?? "").not.toBe("");
    expect(zone).toHaveAccessibleDescription("Displayed in the sidebar. Max 64 KB");

    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.name).toBe("platform_logo");
    expect(screen.getByLabelText("Logo", { selector: 'input[type="file"]' })).toBe(input);
  });

  it("gives two uploaders two different accessible names", () => {
    renderWithProviders(
      <>
        <FileUpload label="Favicon" value={null} onChange={vi.fn()} />
        <FileUpload label="Social Preview Image" value={null} onChange={vi.fn()} />
      </>,
    );
    expect(screen.getByRole("button", { name: "Favicon" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Social Preview Image" })).toBeTruthy();
  });

  // The library Icon renders inline-block align-middle: inside a block parent
  // its line box stretched the rounded-full circle into an oval (UX r1 B1 on
  // #10728). The fixed-shape wrapper must be a centring flex box.
  it("keeps the empty-state icon circle a centring flex box", () => {
    renderWithProviders(<FileUpload label="Logo" value={null} onChange={vi.fn()} />);
    const circle = screen.getByRole("button", { name: "Logo" }).querySelector(".rounded-full");
    expect(circle).not.toBeNull();
    expect(circle?.querySelector("svg")).not.toBeNull();
    // jsdom has no layout: the shape is pinned by the selector the fix relies on.
    expect(circle?.matches(".flex.items-center.justify-center")).toBe(true);
  });

  it("shows the info bubble when given", () => {
    renderWithProviders(<FileUpload label="Logo" info="What it is" value={null} onChange={vi.fn()} />);
    expect(screen.getByLabelText("About this field")).toBeTruthy();
  });

  it("ties Replace and Remove to the field in the filled state", () => {
    renderWithProviders(<FileUpload label="Logo" value="https://example.test/logo.png" onChange={vi.fn()} />);
    const replace = screen.getByRole("button", { name: /Replace/ });
    expect(replace).toHaveAccessibleDescription("Logo");
    expect(screen.getByRole("button", { name: "Remove file" })).toBeTruthy();
  });
});
