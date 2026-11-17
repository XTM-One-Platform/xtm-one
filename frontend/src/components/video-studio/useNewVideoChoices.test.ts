// "New video": free style until something asks for a template, and a kind that suggests without overriding.

import { describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";

import type { VideoTemplateSummary } from "@/types";

import { FREESTYLE, useNewVideoChoices } from "./useNewVideoChoices";

const template = (id: string, extra: Partial<VideoTemplateSummary> = {}) =>
  ({ id, name: id, purposes: ["product_demo"], aspect_ratios: ["16:9", "9:16"], is_default: false, is_builtin: true, ...extra }) as VideoTemplateSummary;

const PRODUCT = template("product", { is_default: true });
const SOCIAL = template("social", { purposes: ["social"], aspect_ratios: ["9:16", "1:1"] });

type Props = { open: boolean; rows: VideoTemplateSummary[]; templateId?: string | null };

function render(initial: Props) {
  return renderHook(({ open, rows, templateId }: Props) => useNewVideoChoices(open, rows, templateId), { initialProps: initial });
}

describe("useNewVideoChoices", () => {
  it("starts in free style, the platform's default template included", () => {
    const hook = render({ open: true, rows: [] });
    expect(hook.result.current.pick).toBe(FREESTYLE);
    hook.rerender({ open: true, rows: [SOCIAL, PRODUCT] });
    expect(hook.result.current.pick).toBe(FREESTYLE);
    expect(hook.result.current.picked).toBeNull();
    expect(hook.result.current.ratio).toBe("16:9");
  });

  it("opens on the template a template page asked for, with the kind it is made for, once it arrives", () => {
    const hook = render({ open: true, rows: [], templateId: "social" });
    expect(hook.result.current.pick).toBe(FREESTYLE);
    hook.rerender({ open: true, rows: [PRODUCT, SOCIAL], templateId: "social" });
    expect(hook.result.current.pick).toBe("social");
    expect(hook.result.current.kind).toBe("social");
    expect(hook.result.current.ratio).toBe("9:16");
  });

  it("keeps the person's choice and brief when the templates refetch", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.setBrief("A launch teaser for the new release"));
    act(() => hook.result.current.choosePick("social"));
    hook.rerender({ open: true, rows: [PRODUCT, SOCIAL, template("other")] });
    expect(hook.result.current.pick).toBe("social");
    expect(hook.result.current.brief).toBe("A launch teaser for the new release");
  });

  it("suggests the template and the format made for a kind, and unpicking the kind takes them back", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.chooseKind("social"));
    expect(hook.result.current.pick).toBe("social");
    expect(hook.result.current.suggested?.id).toBe("social");
    expect(hook.result.current.ratio).toBe("9:16");
    act(() => hook.result.current.chooseKind("social"));
    expect(hook.result.current.kind).toBeNull();
    expect(hook.result.current.pick).toBe(FREESTYLE);
    expect(hook.result.current.ratio).toBe("16:9");
  });

  it("never replaces a look or a format the person chose", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.choosePick(FREESTYLE));
    act(() => hook.result.current.setRatio("1:1"));
    act(() => hook.result.current.chooseKind("social"));
    expect(hook.result.current.pick).toBe(FREESTYLE);
    expect(hook.result.current.ratio).toBe("1:1");
    act(() => hook.result.current.choosePick("product"));
    act(() => hook.result.current.chooseKind("product_demo"));
    expect(hook.result.current.pick).toBe("product");
  });

  it("makes a creative kind in free style, with no template suggested", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.chooseKind("fun"));
    expect(hook.result.current.kind).toBe("fun");
    expect(hook.result.current.suggested).toBeNull();
    expect(hook.result.current.pick).toBe(FREESTYLE);
    expect(hook.result.current.ratios).toEqual(["16:9", "9:16", "1:1", "4:5"]);
  });

  it("falls back when the template picked is gone after a refetch", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.choosePick("social"));
    hook.rerender({ open: true, rows: [PRODUCT] });
    expect(hook.result.current.pick).toBe(FREESTYLE);
  });

  it("starts afresh when the dialog opens again", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.setBrief("Old brief"));
    act(() => hook.result.current.choosePick("social"));
    act(() => hook.result.current.chooseKind("fun"));
    hook.rerender({ open: false, rows: [PRODUCT, SOCIAL] });
    hook.rerender({ open: true, rows: [PRODUCT, SOCIAL] });
    expect(hook.result.current.brief).toBe("");
    expect(hook.result.current.kind).toBeNull();
    expect(hook.result.current.pick).toBe(FREESTYLE);
  });

  it("keeps the format among the ones the picked template makes", () => {
    const hook = render({ open: true, rows: [PRODUCT, SOCIAL] });
    act(() => hook.result.current.setRatio("16:9"));
    act(() => hook.result.current.choosePick("social"));
    expect(hook.result.current.ratios).toEqual(["9:16", "1:1"]);
    expect(hook.result.current.ratio).toBe("9:16");
  });
});
