import { describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DateFilterInput } from "@/components/ui/date-filter-input";
import { renderWithProviders } from "@/testing/providers";

/**
 * The date field of the list pages' filter rows: what it shows for the day in
 * the URL, and when it tells the page a day changed - once committed, never
 * on each keystroke, never for a blur that changed nothing.
 */

function Harness({ initial = "", onChange }: { initial?: string; onChange: (value: string) => void }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <DateFilterInput
        aria-label="From"
        value={value}
        onChange={(next) => {
          onChange(next);
          setValue(next);
        }}
      />
      <button type="button" onClick={() => setValue("")}>
        Reset
      </button>
    </>
  );
}

describe("DateFilterInput", () => {
  it("shows the URL's day in the locale's pattern", () => {
    renderWithProviders(<Harness initial="2026-10-01" onChange={vi.fn()} />);
    expect(screen.getByRole("textbox", { name: "From" })).toHaveValue("10/01/2026");
  });

  it("reports a typed day once, when the field is left", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Harness onChange={onChange} />);

    await user.type(screen.getByRole("textbox", { name: "From" }), "10/05/2026");
    expect(onChange).not.toHaveBeenCalled();

    await user.tab();
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith("2026-10-05");
    expect(screen.getByRole("textbox", { name: "From" })).toHaveValue("10/05/2026");
  });

  it("stays silent when the field is left unchanged", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Harness initial="2026-10-01" onChange={onChange} />);

    await user.click(screen.getByRole("textbox", { name: "From" }));
    await user.tab();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("reports an empty day when cleared", async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<Harness initial="2026-10-01" onChange={onChange} />);

    await user.click(screen.getByRole("button", { name: "Clear date" }));
    expect(onChange).toHaveBeenCalledWith("");
    expect(screen.getByRole("textbox", { name: "From" })).toHaveValue("");
  });

  it("empties when the page resets its filters", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness initial="2026-10-01" onChange={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByRole("textbox", { name: "From" })).toHaveValue("");
  });
});
