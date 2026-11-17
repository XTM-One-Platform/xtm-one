import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";

import { TagInput } from "@/components/ui/tag-input";
import { renderWithProviders } from "@/testing/providers";

describe("TagInput disabled state", () => {
  it("shows the No tags text and no text field when disabled and empty", () => {
    renderWithProviders(
      <TagInput value={[]} onChange={() => {}} disabled suggestFromPlatformTags={false} />,
    );
    expect(screen.getByText("No tags")).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("lists the tags without remove buttons when disabled", () => {
    renderWithProviders(
      <TagInput value={["malware", "apt"]} onChange={() => {}} disabled suggestFromPlatformTags={false} />,
    );
    expect(screen.getByText("malware")).toBeTruthy();
    expect(screen.getByText("apt")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Remove tag" })).toBeNull();
    expect(screen.queryByText("No tags")).toBeNull();
  });

  it("offers the text field and remove buttons when enabled", () => {
    renderWithProviders(
      <TagInput value={["malware"]} onChange={() => {}} suggestFromPlatformTags={false} />,
    );
    expect(screen.getByRole("textbox")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove tag" })).toBeTruthy();
    expect(screen.queryByText("No tags")).toBeNull();
  });
});

describe("TagInput labelling", () => {
  it("puts the id on the typing box so a label names it, and ties the help line", () => {
    renderWithProviders(
      <>
        <label htmlFor="tags-field">Tags</label>
        <TagInput id="tags-field" aria-describedby="tags-field-hint" value={[]} onChange={() => {}} suggestFromPlatformTags={false} />
        <p id="tags-field-hint">Press Enter after each tag.</p>
      </>,
    );
    const box = screen.getByLabelText("Tags");
    expect(box.getAttribute("id")).toBe("tags-field");
    expect(box.getAttribute("aria-describedby")).toBe("tags-field-hint");
  });
});
