// `t_i18n`: a label looked up by a value the server sent may be missing; formatjs throws on a missing id,
// so the formatter logs it and renders nothing rather than taking the whole app down.

import { describe, expect, it, vi } from "vitest";
import { renderHook } from "@testing-library/react";

import { testQueryClient, withProviders } from "@/testing/providers";
import { useFormatter } from "@/i18n";

describe("useFormatter", () => {
  it("renders nothing for a missing message id, and still formats the others", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const { result } = renderHook(() => useFormatter(), { wrapper: withProviders(testQueryClient()) });
    expect(result.current.t_i18n(undefined as unknown as string)).toBe("");
    expect(error).toHaveBeenCalledWith("[i18n] t_i18n called without a message id");
    expect(result.current.t_i18n("{count} commands", { count: 2 })).toBe("2 commands");
    error.mockRestore();
  });
});
