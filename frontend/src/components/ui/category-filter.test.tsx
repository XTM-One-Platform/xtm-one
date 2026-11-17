// CategoryFilter: segments by default, one Category filter pill when collapsed
// (data table standard v2: a category is a pill, never a plain select).

import { describe, expect, it } from "vitest";
import { fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/testing/providers";
import { CategoryFilter } from "./category-filter";

const CATEGORIES = ["mail", "code", "chat"];

describe("CategoryFilter", () => {
  it("draws one button per category plus All by default", () => {
    renderWithProviders(<CategoryFilter categories={CATEGORIES} selected={null} onChange={() => {}} />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("draws one Category pill and no select when collapsed", () => {
    renderWithProviders(<CategoryFilter collapsed categories={CATEGORIES} selected={null} onChange={() => {}} />);
    expect(screen.queryAllByRole("button", { name: "mail" })).toHaveLength(0);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("button").textContent).toContain("Category");
  });

  it("shows the selected category's label on the pill when collapsed", () => {
    renderWithProviders(
      <CategoryFilter collapsed categories={CATEGORIES} selected="code" labels={{ code: "Code hosting" }} onChange={() => {}} />,
    );
    expect(screen.getByRole("button").textContent).toContain("Code hosting");
  });

  it("picks and clears a category from the pill's panel", () => {
    const picked: (string | null)[] = [];
    renderWithProviders(
      <CategoryFilter collapsed categories={CATEGORIES} selected="code" onChange={(cat) => picked.push(cat)} />,
    );
    fireEvent.click(screen.getByRole("button"));
    fireEvent.click(screen.getByRole("button", { name: "mail" }));
    fireEvent.click(screen.getByRole("button"));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(picked).toEqual(["mail", null]);
  });
});
