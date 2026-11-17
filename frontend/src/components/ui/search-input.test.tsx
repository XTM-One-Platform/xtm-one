import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { SearchInput } from "@/components/ui/search-input";
import { renderWithProviders } from "@/testing/providers";

describe("SearchInput accessible name", () => {
  it("names the input after its placeholder when no label is given", () => {
    renderWithProviders(<SearchInput placeholder="Filter skills..." value="" onChange={() => {}} />);
    const input = screen.getByRole("searchbox", { name: "Filter skills..." });
    expect(input.getAttribute("placeholder")).toBe("Filter skills...");
  });

  it("uses the explicit label over the placeholder", () => {
    renderWithProviders(
      <SearchInput placeholder="Filter..." aria-label="Filter agent skills" value="" onChange={() => {}} />,
    );
    expect(screen.getByRole("searchbox", { name: "Filter agent skills" })).toBeTruthy();
    expect(screen.queryByRole("searchbox", { name: "Filter..." })).toBeNull();
  });

  it("keeps its name and placeholder as a toolbar search", () => {
    renderWithProviders(<SearchInput toolbar placeholder="Search prompts..." value="" onChange={() => {}} />);
    const input = screen.getByRole("searchbox", { name: "Search prompts..." });
    expect(input.getAttribute("placeholder")).toBe("Search prompts...");
  });

  it("sizes a toolbar search from a hidden copy of its placeholder, not an inline style", () => {
    const { container } = renderWithProviders(
      <SearchInput toolbar placeholder="Search prompts..." value="" onChange={() => {}} />,
    );
    const sizer = screen.getByTestId("search-input-sizer");
    expect(sizer.textContent).toBe("Search prompts...");
    expect(sizer.getAttribute("aria-hidden")).toBe("true");
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("drops the placeholder floor to a fixed 128 px sizer in compact toolbar mode", () => {
    renderWithProviders(<SearchInput toolbar compact placeholder="Search integrations..." value="" onChange={() => {}} />);
    expect(screen.getByTestId("search-input-sizer").getAttribute("data-floor")).toBe("fixed");
  });

  it("shows the short placeholder in compact toolbar mode and keeps the full text as the name", () => {
    renderWithProviders(<SearchInput toolbar compact placeholder="Search integrations..." value="" onChange={() => {}} />);
    const input = screen.getByRole("searchbox", { name: "Search integrations..." });
    expect(input.getAttribute("placeholder")).toBe("Search...");
  });

  it("keeps an explicit label in compact toolbar mode", () => {
    renderWithProviders(
      <SearchInput toolbar compact placeholder="Search integrations..." aria-label="Find an integration" value="" onChange={() => {}} />,
    );
    const input = screen.getByRole("searchbox", { name: "Find an integration" });
    expect(input.getAttribute("placeholder")).toBe("Search...");
  });

  it("keeps the full placeholder when compact is set outside a toolbar", () => {
    renderWithProviders(<SearchInput compact placeholder="Search integrations..." value="" onChange={() => {}} />);
    const input = screen.getByRole("searchbox", { name: "Search integrations..." });
    expect(input.getAttribute("placeholder")).toBe("Search integrations...");
  });

  it("keeps the placeholder floor outside compact mode", () => {
    renderWithProviders(<SearchInput toolbar placeholder="Search integrations..." value="" onChange={() => {}} />);
    expect(screen.getByTestId("search-input-sizer").getAttribute("data-floor")).toBe("placeholder");
  });

  it("renders no sizer outside a toolbar", () => {
    renderWithProviders(<SearchInput placeholder="Search prompts..." value="" onChange={() => {}} />);
    expect(screen.queryByTestId("search-input-sizer")).toBeNull();
  });

  it("marks the toolbar wrapper and keeps the sizer as its only min-content source", () => {
    const { container } = renderWithProviders(
      <SearchInput toolbar placeholder="Search presentations..." value="" onChange={() => {}} />,
    );
    const wrapper = container.querySelector('[data-toolbar-search="true"]');
    expect(wrapper).not.toBeNull();
    const sizer = screen.getByTestId("search-input-sizer");
    const input = screen.getByRole("searchbox", { name: "Search presentations..." });
    expect(wrapper?.contains(sizer)).toBe(true);
    expect(wrapper?.contains(input)).toBe(true);
    expect(sizer.parentElement).toBe(wrapper);
  });

  it("marks the toolbar field as shrinkable so the input's intrinsic width never sets the floor", () => {
    const { container } = renderWithProviders(
      <SearchInput toolbar placeholder="Search presentations..." value="" onChange={() => {}} />,
    );
    const field = container.querySelector('[data-toolbar-field="shrinkable"]');
    expect(field).not.toBeNull();
    expect(field?.contains(screen.getByRole("searchbox", { name: "Search presentations..." }))).toBe(true);
  });

  it("does not mark a search outside a toolbar", () => {
    const { container } = renderWithProviders(
      <SearchInput placeholder="Search prompts..." value="" onChange={() => {}} />,
    );
    expect(container.querySelector("[data-toolbar-search]")).toBeNull();
    expect(container.querySelector("[data-toolbar-field]")).toBeNull();
  });

  it("still hands the typed value to onChange", () => {
    const onChange = vi.fn();
    renderWithProviders(<SearchInput placeholder="Filter skills..." value="" onChange={onChange} />);
    fireEvent.change(screen.getByRole("searchbox", { name: "Filter skills..." }), { target: { value: "cti" } });
    expect(onChange).toHaveBeenCalledWith("cti");
  });
});
