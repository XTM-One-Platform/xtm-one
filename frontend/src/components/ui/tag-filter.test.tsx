import { describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { TagFilter } from "./tag-filter";

const TAGS = ["cti", "phishing", "ransomware", "triage", "vulnerability", "osint", "malware", "reporting", "threat-hunting", "ioc"];

describe("TagFilter collapsed", () => {
  it("draws one Tags pill and no tag chip inline", () => {
    renderWithProviders(<TagFilter collapsed allTags={TAGS} selected={[]} onChange={() => {}} />);

    const pill = screen.getByRole("button", { name: "Tags" });
    expect(pill.getAttribute("aria-expanded")).toBe("false");
    for (const tag of TAGS) expect(screen.queryByRole("button", { name: tag })).toBeNull();
  });

  it("opens every tag in its panel and toggles one", () => {
    const onChange = vi.fn();
    renderWithProviders(<TagFilter collapsed allTags={TAGS} selected={["cti"]} onChange={onChange} />);

    fireEvent.click(screen.getByRole("button", { name: /Tags/ }));
    const panel = screen.getByRole("group", { name: "Tags" });
    expect(panel).toBeTruthy();
    for (const tag of TAGS) expect(screen.getByRole("button", { name: tag })).toBeTruthy();
    expect(screen.getByRole("button", { name: "cti" }).getAttribute("aria-pressed")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "osint" }));
    expect(onChange).toHaveBeenCalledWith(["cti", "osint"]);
  });

  it("shows how many tags narrow the list and clears them", () => {
    const onChange = vi.fn();
    renderWithProviders(<TagFilter collapsed allTags={TAGS} selected={["cti", "ioc"]} onChange={onChange} />);

    const pill = screen.getByRole("button", { name: /Tags/ });
    expect(pill.textContent).toContain("2");

    fireEvent.click(pill);
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it("keeps the inline chips when not collapsed", () => {
    renderWithProviders(<TagFilter allTags={TAGS.slice(0, 3)} selected={[]} onChange={() => {}} />);

    expect(screen.getByRole("button", { name: "cti" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Tags" })).toBeNull();
  });
});
