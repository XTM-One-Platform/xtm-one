// The one chat image lightbox: dialog semantics, Escape, focus trap,
// focus return and body scroll lock, and no second lightbox anywhere in src/.

import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, sep } from "node:path";

import { renderWithProviders } from "@/testing/providers";
import { ImageLightbox } from "./ImageLightbox";

const PNG = "data:image/png;base64,iVBORw0KGgo=";

afterEach(() => {
  document.body.style.overflow = "";
});

describe("ImageLightbox", () => {
  it("is a named modal dialog that Escape closes", () => {
    const onClose = vi.fn();
    renderWithProviders(<ImageLightbox src={PNG} alt="Q3 revenue" onClose={onClose} />);

    const dialog = screen.getByRole("dialog", { name: "Q3 revenue" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("names an image with no alt text", () => {
    renderWithProviders(<ImageLightbox src={PNG} alt="  " onClose={() => {}} />);
    expect(screen.getByRole("dialog", { name: "Image preview" })).toBeInTheDocument();
  });

  it("keeps Tab and Shift+Tab inside the dialog, across its own buttons", () => {
    renderWithProviders(
      <ImageLightbox
        src={PNG}
        alt="report.png"
        onClose={() => {}}
        actions={<button type="button">Download report.png</button>}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "report.png" });
    const download = screen.getByRole("button", { name: "Download report.png" });
    const close = screen.getByRole("button", { name: "Close" });
    expect(close).toHaveFocus();

    fireEvent.keyDown(document, { key: "Tab" });
    expect(download).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(download).toHaveFocus();
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
  });

  it("locks the body scroll while open and gives back the previous value on close, focus back on the opener", () => {
    document.body.style.overflow = "auto";
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = renderWithProviders(<ImageLightbox src={PNG} alt="Q3" onClose={() => {}} />);
    expect(document.body.style.overflow).toBe("hidden");

    unmount();
    expect(document.body.style.overflow).toBe("auto");
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("renders outside the page's scrollers, so the transcript cannot scroll behind it", () => {
    renderWithProviders(
      <div aria-label="Conversation transcript" tabIndex={0} className="overflow-y-auto">
        <ImageLightbox src={PNG} alt="Q3" onClose={() => {}} />
      </div>,
    );
    const transcript = screen.getByLabelText("Conversation transcript");
    const dialog = screen.getByRole("dialog", { name: "Q3" });
    expect(transcript).not.toContainElement(dialog);
    expect(dialog.parentElement).toBe(document.body);
  });

  it("keeps the focus in the dialog when the image or the scrim is pressed", () => {
    renderWithProviders(<ImageLightbox src={PNG} alt="Q3" onClose={() => {}} />);
    const image = screen.getByRole("img", { name: "Q3" });
    // fireEvent returns false when the default (moving the focus) was prevented.
    expect(fireEvent.mouseDown(image)).toBe(false);
    expect(fireEvent.mouseDown(screen.getByRole("dialog", { name: "Q3" }))).toBe(false);
    expect(fireEvent.mouseDown(screen.getByRole("button", { name: "Close" }))).toBe(true);
  });

  it("closes on a click outside the image, not on the image", () => {
    const onClose = vi.fn();
    renderWithProviders(<ImageLightbox src={PNG} alt="Q3" onClose={onClose} />);
    fireEvent.click(screen.getByRole("img", { name: "Q3" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("dialog", { name: "Q3" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

// vitest runs from frontend/ (CI and `yarn test`), as no-hand-rolled-menu.test.ts.
const SRC = join(process.cwd(), "src");
const SELF = join(SRC, "components/chat/ImageLightbox.tsx");

function* sourceFiles(dir: string): Generator<string> {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "node_modules") yield* sourceFiles(full);
    } else if (/\.tsx$/.test(entry.name) && !/\.test\.tsx$/.test(entry.name) && full !== SELF) {
      yield full;
    }
  }
}

describe("one chat image lightbox", () => {
  it("no other file under src/ renders its own full-size image overlay", () => {
    const offenders: string[] = [];
    for (const file of sourceFiles(SRC)) {
      if (/max-h-\[90vh\] max-w-\[90vw\]/.test(readFileSync(file, "utf8"))) {
        offenders.push(relative(SRC, file).split(sep).join("/"));
      }
    }
    expect(offenders, `own image lightbox in: ${offenders.join(", ")}`).toEqual([]);
  });

  it("its scrim buttons ring with the scrim token, never ring-focus", () => {
    const source = readFileSync(SELF, "utf8");
    expect(source).toMatch(/focus-visible:ring-on-scrim\b/);
    expect(source).not.toMatch(/focus-visible:ring-focus\b/);
  });

  it("the chat's markdown images open it", () => {
    expect(readFileSync(join(SRC, "components/chat/ChatMarkdown.tsx"), "utf8")).toMatch(/<ImageLightbox\b/);
  });
});
