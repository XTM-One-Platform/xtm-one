import { test, expect, type Locator, type Page } from '@playwright/test';
import { createAdminApiContext } from './helpers';

// ---------------------------------------------------------------------------
// Presentations — operating a deck from the toolbar and the keyboard.
//
// Driven through a template preview: a deck built client-side from a seeded
// template, so no LLM call and no persisted presentation are needed. The
// preview renders the same `DeckStage` as the chat card and the standalone
// viewer — one toolbar along the top edge, nothing drawn over the slide, the
// same keys — so what is asserted here holds for a real deck too.
// ---------------------------------------------------------------------------

/**
 * The counter button reads `3 / 12` and is named "Slide 3 of 12. Go to slide".
 * Until the app's deck controller holds the reveal instance it reads
 * "Slide 1 of 0": a total of 0 is that placeholder, never a deck.
 */
const COUNTER = /^Slide (\d+) of ([1-9]\d*)\./;

async function findBuiltinPresentationTemplateId(): Promise<string> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const resp = await apiCtx.get('/api/v1/template-containers?kind=presentation', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok()) return '';
    const containers: { id: string; is_builtin: boolean }[] = await resp.json();
    return containers.find((c) => c.is_builtin)?.id ?? '';
  } finally {
    await apiCtx.dispose();
  }
}

async function readCounter(toolbar: Locator): Promise<{ current: number; total: number }> {
  const label = await toolbar.getByRole('button', { name: COUNTER }).getAttribute('aria-label');
  const match = label?.match(COUNTER);
  if (!match) throw new Error(`Counter button not found in the toolbar (aria-label: ${label})`);
  return { current: Number(match[1]), total: Number(match[2]) };
}

async function expectSlide(toolbar: Locator, current: number): Promise<void> {
  await expect(toolbar.getByRole('button', { name: new RegExp(`^Slide ${current} of [1-9]\\d*\\.`) })).toBeVisible();
}

async function openPreview(page: Page, templateId: string): Promise<{ dialog: Locator; toolbar: Locator }> {
  await page.goto(`/templates/${templateId}`);
  await page.getByRole('button', { name: 'Preview' }).click();

  const dialog = page.getByRole('dialog', { name: 'Template preview' });
  await expect(dialog).toBeVisible();
  // The stage is code-split; the toolbar appears once the chunk has loaded,
  // and reveal marks the deck usable with `.ready`.
  const toolbar = dialog.getByRole('toolbar', { name: 'Presentation controls' });
  await expect(toolbar).toBeVisible();
  await expect(dialog.locator('.reveal.ready')).toBeVisible();
  // `.ready` is reveal's own flag, set inside `initialize()`: the keys already
  // work, but the app's controller receives the instance a render later, and
  // until then the dialog's Escape guard has no deck to ask about the
  // overview. The counter naming the deck's real length is that hand-over.
  await expectSlide(toolbar, 1);
  return { dialog, toolbar };
}

test.describe('Presentations — deck toolbar and keyboard', () => {
  let templateId = '';

  test.beforeAll(async () => {
    templateId = await findBuiltinPresentationTemplateId();
  });

  test.beforeEach(() => {
    test.skip(!templateId, 'No built-in presentation template seeded on this platform');
  });

  test('should draw every control in the toolbar and nothing over the slide', async ({ page }) => {
    const { dialog, toolbar } = await openPreview(page, templateId);

    await expect(toolbar.getByRole('button', { name: 'Previous slide' })).toBeVisible();
    await expect(toolbar.getByRole('button', { name: 'Next slide' })).toBeVisible();
    await expect(toolbar.getByRole('button', { name: COUNTER })).toBeVisible();
    await expect(toolbar.getByRole('button', { name: 'More actions' })).toBeVisible();

    // reveal's own chrome is off: no corner arrows, no progress bar, no
    // on-slide slide number (reveal still mounts the elements; `toBeHidden`
    // accepts both an absent and a `display: none` element).
    await expect(dialog.locator('.reveal .controls')).toBeHidden();
    await expect(dialog.locator('.reveal .progress')).toBeHidden();
    await expect(dialog.locator('.reveal .slide-number')).toBeHidden();

    // The rarer presenter actions live in the More menu, each with its key.
    await toolbar.getByRole('button', { name: 'More actions' }).click();
    const menu = page.getByRole('menu', { name: 'More actions' });
    await expect(menu.getByRole('menuitem', { name: /Slide overview/ })).toBeVisible();
    await expect(menu.getByRole('menuitem', { name: /Black screen/ })).toBeVisible();
    await expect(menu.getByRole('menuitem', { name: /Keyboard shortcuts/ })).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(menu).toBeHidden();
    // Escape closed the menu only — the preview is still open.
    await expect(dialog).toBeVisible();
  });

  test('should turn the page from the toolbar and follow it in the counter', async ({ page }) => {
    const { toolbar } = await openPreview(page, templateId);
    const { total } = await readCounter(toolbar);
    test.skip(total < 2, 'The seeded template declares a single layout');

    await toolbar.getByRole('button', { name: 'Next slide' }).click();
    await expectSlide(toolbar, 2);
    await toolbar.getByRole('button', { name: 'Previous slide' }).click();
    await expectSlide(toolbar, 1);
  });

  test('should turn the page from the keyboard, focus on a toolbar button included', async ({ page }) => {
    const { toolbar } = await openPreview(page, templateId);
    const { total } = await readCounter(toolbar);
    test.skip(total < 2, 'The seeded template declares a single layout');

    // The preview owns the keys from the moment it opens: no click needed.
    await page.keyboard.press('ArrowRight');
    await expectSlide(toolbar, 2);
    await page.keyboard.press('ArrowLeft');
    await expectSlide(toolbar, 1);
    await page.keyboard.press('End');
    await expectSlide(toolbar, total);
    await page.keyboard.press('Home');
    await expectSlide(toolbar, 1);

    // A toolbar button holding the focus does not swallow the arrows (a media
    // player's transport keys work wherever the focus is) — but Space on it
    // activates THAT button once, never the button and the deck both.
    const next = toolbar.getByRole('button', { name: 'Next slide' });
    await next.focus();
    await page.keyboard.press('ArrowRight');
    await expectSlide(toolbar, 2);
    await page.keyboard.press('Space');
    await expectSlide(toolbar, 3 <= total ? 3 : 2);
  });

  test('should open the go-to-slide menu on G and jump to a typed number', async ({ page }) => {
    const { dialog, toolbar } = await openPreview(page, templateId);
    const { total } = await readCounter(toolbar);
    test.skip(total < 3, 'The seeded template declares fewer than three layouts');

    await page.keyboard.press('g');
    const list = dialog.getByRole('listbox', { name: 'Go to slide' });
    await expect(list).toBeVisible();
    // Every slide is listed, the number field has the focus.
    await expect(list.getByRole('option')).toHaveCount(total);
    const field = dialog.getByRole('spinbutton', { name: 'Slide number' });
    await expect(field).toBeFocused();

    // Typing a number inside the menu never turns the page underneath.
    await field.fill('3');
    await expectSlide(toolbar, 1);
    await page.keyboard.press('Enter');
    await expect(list).toBeHidden();
    await expectSlide(toolbar, 3);

    // Picking a slide from the list with the mouse.
    await toolbar.getByRole('button', { name: COUNTER }).click();
    await expect(list).toBeVisible();
    await list.getByRole('option').first().click();
    await expect(list).toBeHidden();
    await expectSlide(toolbar, 1);
  });

  test('should open the shortcut sheet on ? and close it without closing the preview', async ({ page }) => {
    const { dialog } = await openPreview(page, templateId);

    await page.keyboard.press('Shift+/');
    const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText('Go to slide')).toBeVisible();
    await expect(sheet.getByText('Fullscreen', { exact: true })).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(sheet).toBeHidden();
    await expect(dialog).toBeVisible();

    // Escape pressed the moment the sheet is drawn, before Radix has made it
    // the top layer, still closes the sheet alone.
    await page.keyboard.press('Shift+/');
    await page.keyboard.press('Escape');
    await expect(sheet).toBeHidden();
    await expect(dialog).toBeVisible();

    // The same sheet from the More menu.
    await dialog.getByRole('button', { name: 'More actions' }).click();
    await page.getByRole('menuitem', { name: /Keyboard shortcuts/ }).click();
    await expect(sheet).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(sheet).toBeHidden();
    await expect(dialog).toBeVisible();
  });

  test('should let Escape leave the slide overview before it closes the preview', async ({ page }) => {
    const { dialog } = await openPreview(page, templateId);
    const reveal = dialog.locator('.reveal.ready');
    const stage = dialog.locator('.deck-stage');

    await page.keyboard.press('o');
    await expect(reveal).toHaveClass(/overview/);
    // reveal sets its class synchronously; the app learns of the overview a
    // render later, from `overviewshown`. Escape is pressed once the app holds
    // it too, so the dialog's guard has the overview to answer with.
    await expect(stage).toHaveAttribute('data-deck-overview', 'true');

    // First Escape: the deck has something to leave, the dialog stays.
    await page.keyboard.press('Escape');
    await expect(stage).not.toHaveAttribute('data-deck-overview', 'true');
    await expect(reveal).not.toHaveClass(/overview/);
    await expect(dialog).toBeVisible();

    // Second Escape: nothing left to leave, the dialog closes.
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  });

  test('should black out the screen on B and resume on B', async ({ page }) => {
    const { dialog } = await openPreview(page, templateId);
    const reveal = dialog.locator('.reveal.ready');

    await page.keyboard.press('b');
    await expect(reveal).toHaveClass(/paused/);
    await page.keyboard.press('b');
    await expect(reveal).not.toHaveClass(/paused/);
  });

  // Logo placement: the resolver's answer is pinned per layout in
  // `tests_unit/logo-position.test.ts`; this is the rendered half - the position the bar
  // publishes as `data-logo-position`, and where the mark actually lands in
  // the row the exports and the layout audit read. The preview deck opens on a
  // left-aligned title (auto -> start), runs through body slides (always the
  // corner) and carries a `closing` slide (auto -> center) somewhere in the
  // template's layout order - the brand decks advertise the v1 layouts AFTER
  // it, so it is located by its `data-layout`, never assumed to be the last.
  test('should place the logo with the title on the opening and closing slides', async ({ page }) => {
    const { dialog, toolbar } = await openPreview(page, templateId);
    const { total } = await readCounter(toolbar);
    // Every slide is in the DOM (reveal toggles `display` on the sections), so
    // the layout order is read once, before any assertion: a skip here says
    // what the template lacks instead of hiding assertions that already passed.
    const layouts = await dialog
      .locator('.reveal .slides .deck-slide')
      .evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.layout ?? ''));
    const closingPosition = layouts.indexOf('closing') + 1;
    test.skip(total < 3, 'The seeded template declares fewer than three layouts');
    test.skip(closingPosition === 0, 'The seeded template advertises no closing layout');

    const current = dialog.locator('.reveal section.present');
    const frame = current.locator('.deck-slide');
    const bar = current.locator('.deck-chrome');

    /** Everything the placement assertions compare, read in ONE frame: two
     *  `boundingBox()` calls are two instants, and the logo is only comparable
     *  with its row at the same one. `moving` counts the transitions still
     *  pending or running on the slide's section. The whole reading is the
     *  failure message. */
    const readRow = () =>
      current.evaluate((section) => {
        const box = (el: Element | null | undefined) => {
          if (!el) return null;
          const rect = el.getBoundingClientRect();
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
        };
        const row = section.querySelector('.deck-chrome');
        const img = row?.querySelector<HTMLImageElement>('.deck-chrome-logo');
        return {
          moving: section.getAnimations().length,
          row: box(row),
          logo: box(img),
          number: box(row?.querySelector('.deck-chrome-number')),
          image: img ? { complete: img.complete, naturalWidth: img.naturalWidth, naturalHeight: img.naturalHeight } : null,
          fonts: document.fonts.status,
        };
      });
    type RowReading = Awaited<ReturnType<typeof readRow>>;

    /** The incoming slide travels for reveal's 0.8s `slide` transition, and the
     *  counter, the layout attribute and `section.present` all switch before it
     *  moves: a transition keeps its start box until its first frame, which can
     *  come a few hundred ms late, so two equal readings alone do not mean the
     *  slide arrived. At rest is no transition left on the section AND the same
     *  row box twice. */
    const atRest = async () => {
      let previous = '';
      await expect
        .poll(
          async () => {
            const reading = await readRow();
            const row = JSON.stringify(reading.row);
            const still = reading.moving === 0 && row === previous;
            previous = row;
            return still;
          },
          { message: 'The slide never came to rest', timeout: 5_000, intervals: [100] },
        )
        .toBe(true);
    };

    /** The row once its logo has DRAWN, or null when the template ships no
     *  logo asset or the bytes never arrive: the position attribute is asserted
     *  either way, the geometry only on a drawn mark. Drawn means `complete`
     *  AND `naturalWidth > 0` - `complete` alone is also true of a failed load,
     *  whose empty box would measure as a mark. */
    const drawnRow = async () => {
      const drawn = (reading: RowReading) =>
        !!reading.image && reading.image.complete && reading.image.naturalWidth > 0;
      // The `<img>` mounts once the asset URL resolved (a JWT-authenticated
      // download), then loads: wait for both, bounded.
      const deadline = Date.now() + 10_000;
      let reading = await readRow();
      while (!drawn(reading) && Date.now() < deadline) {
        await page.waitForTimeout(250);
        reading = await readRow();
      }
      if (!drawn(reading)) {
        const reason = reading.image ? 'Logo asset not drawn' : 'No logo asset on this template';
        test.info().annotations.push({ type: 'note', description: `${reason}; geometry not measured` });
        return null;
      }
      const { logo, row } = reading;
      if (!logo || logo.width === 0) {
        test.info().annotations.push({ type: 'note', description: 'Logo asset has no box; geometry not measured' });
        return null;
      }
      if (!row) throw new Error('The chrome bar has no box');
      return { ...reading, logo, row, explain: `chrome row: ${JSON.stringify(reading)}` };
    };

    // Slide 1: a title slide with no `align` is a left-aligned cover, so the
    // mark sits on the left padding edge (the row's first child), and the
    // title slide carries no number.
    await expect(frame).toHaveAttribute('data-layout', 'title');
    await expect(bar).toHaveAttribute('data-logo-position', 'start');
    await expect(bar.locator('.deck-chrome-number')).toHaveCount(0);
    await atRest();
    const start = await drawnRow();
    if (start) {
      expect(Math.abs(start.logo.x - start.row.x), start.explain).toBeLessThanOrEqual(2);
    }

    // Slide 2: a body or section slide keeps the corner - inside the right
    // group, after the number, flush with the row's right edge.
    await toolbar.getByRole('button', { name: 'Next slide' }).click();
    await expectSlide(toolbar, 2);
    await expect(frame).not.toHaveAttribute('data-layout', /^(title|closing)$/);
    await expect(bar).toHaveAttribute('data-logo-position', 'end');
    await atRest();
    const end = await drawnRow();
    if (end) {
      expect(Math.abs(end.logo.x + end.logo.width - (end.row.x + end.row.width)), end.explain).toBeLessThanOrEqual(2);
      if (end.number) expect(end.number.x + end.number.width, end.explain).toBeLessThanOrEqual(end.logo.x);
    }

    // The closing slide puts the mark on the slide axis, whatever the number on
    // its right measures. Reached by its position through the go-to field.
    await page.keyboard.press('g');
    const field = dialog.getByRole('spinbutton', { name: 'Slide number' });
    await expect(field).toBeFocused();
    await field.fill(String(closingPosition));
    await page.keyboard.press('Enter');
    await expectSlide(toolbar, closingPosition);
    await expect(frame).toHaveAttribute('data-layout', 'closing');
    await expect(bar).toHaveAttribute('data-logo-position', 'center');
    await atRest();
    const center = await drawnRow();
    if (center) {
      const logoAxis = center.logo.x + center.logo.width / 2;
      const rowAxis = center.row.x + center.row.width / 2;
      expect(Math.abs(logoAxis - rowAxis), center.explain).toBeLessThanOrEqual(2);
    }
  });

  // The section slide's ghost number: a watermark centred on the
  // slide's right edge that bleeds past it by design. It is placed by its
  // LAYOUT box, never by a transform - `page.pdf()` paginates on layout boxes,
  // a transform only moves what is painted, and a single line box has no break
  // opportunity: centred with `top: 50%` + `translateY(-50%)` the box crossed
  // the page's bottom edge by 6px, Chromium pushed the whole number to the next
  // page and the slide's overflow clip cut it to its top. This is the rendered
  // half of that contract (the CSS rule itself is pinned in
  // `test_presentation_export.py`): the box the number is drawn in IS its
  // layout box, it lies within the slide, and it is centred on the section.
  test('should keep the section ghost number inside the slide, centred, without a transform', async ({ page }) => {
    const { dialog, toolbar } = await openPreview(page, templateId);
    const layouts = await dialog
      .locator('.reveal .slides .deck-slide')
      .evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.layout ?? ''));
    const sectionPosition = layouts.indexOf('section') + 1;
    test.skip(sectionPosition === 0, 'The seeded template advertises no section layout');

    if (sectionPosition > 1) {
      await page.keyboard.press('g');
      const field = dialog.getByRole('spinbutton', { name: 'Slide number' });
      await expect(field).toBeFocused();
      await field.fill(String(sectionPosition));
      await page.keyboard.press('Enter');
    }
    await expectSlide(toolbar, sectionPosition);
    const frame = dialog.locator('.reveal section.present .deck-slide');
    await expect(frame).toHaveAttribute('data-layout', 'section');
    const number = frame.locator('.deck-section-number');
    // The preview's section slide carries a number, so the watermark is drawn.
    await expect(number).toHaveCount(1);

    type Box = { top: number; bottom: number };
    const geometry = () =>
      frame.evaluate((slide) => {
        const ghost = slide.querySelector('.deck-section-number') as HTMLElement;
        const section = slide.querySelector('.deck-section') as HTMLElement;
        const box = (el: Element): Box => {
          const rect = el.getBoundingClientRect();
          return { top: rect.top, bottom: rect.bottom };
        };
        return { transform: getComputedStyle(ghost).transform, slide: box(slide), section: box(section), number: box(ghost) };
      });
    // Measured once the incoming slide stands still (see `atRest` above).
    let measured = await geometry();
    await expect
      .poll(
        async () => {
          const before = JSON.stringify(measured);
          await page.waitForTimeout(100);
          measured = await geometry();
          return before === JSON.stringify(measured);
        },
        { message: 'The section slide never came to rest', timeout: 5_000 },
      )
      .toBe(true);

    // No transform: what the paginator lays out is what the audience sees.
    expect(measured.transform).toBe('none');
    // The layout box lies within the slide's own box - on every page format.
    expect(measured.number.top).toBeGreaterThanOrEqual(measured.slide.top - 1);
    expect(measured.number.bottom).toBeLessThanOrEqual(measured.slide.bottom + 1);
    // And it is centred on the section body, as the viewer always drew it.
    const numberAxis = (measured.number.top + measured.number.bottom) / 2;
    const sectionAxis = (measured.section.top + measured.section.bottom) / 2;
    expect(Math.abs(numberAxis - sectionAxis)).toBeLessThanOrEqual(1);
  });
});

// ---------------------------------------------------------------------------
// The theme picker as a keyboard radio group, driven through the "New
// presentation" dialog: one tab stop, arrows that wrap and select, Home / End,
// and a scheme the template cannot dress that the keys skip and the tab order
// leaves out. The dialog sits behind the Agentic AI licence gate; a platform
// without one shows the upgrade dialog instead, and these tests skip.
// ---------------------------------------------------------------------------

async function findSingleSchemePresentationTemplate(): Promise<{ id: string; name: string } | null> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const resp = await apiCtx.get('/api/v1/template-containers?kind=presentation', {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!resp.ok()) return null;
    const containers: { id: string; name: string; supported_schemes: string[] }[] = await resp.json();
    const one = containers.find((c) => c.supported_schemes.length === 1);
    return one ? { id: one.id, name: one.name } : null;
  } finally {
    await apiCtx.dispose();
  }
}

/** Clicks the given launcher and returns the dialog, or skips behind the licence gate. */
async function openNewPresentation(page: Page, launcher: Locator): Promise<Locator> {
  await launcher.click();
  const dialog = page.getByRole('dialog', { name: 'New presentation' });
  await expect(page.getByRole('dialog').first()).toBeVisible();
  test.skip(!(await dialog.isVisible()), 'New presentation is behind the Agentic AI licence gate on this platform');
  return dialog;
}

function themeRadios(dialog: Locator): { group: Locator; dark: Locator; light: Locator } {
  const group = dialog.getByRole('radiogroup', { name: 'Theme' });
  return { group, dark: group.getByRole('radio', { name: 'Dark' }), light: group.getByRole('radio', { name: 'Light' }) };
}

test.describe('Presentations - theme picker as a keyboard radio group', () => {
  test('should move the selection with the arrows, Home and End from a single tab stop', async ({ page }) => {
    await page.goto('/presentations');
    const dialog = await openNewPresentation(page, page.getByRole('button', { name: /^New presentation/ }).first());

    // No template: both schemes are open, so the wrap-around is observable.
    const templatePick = dialog.getByRole('combobox', { name: 'Template' });
    await templatePick.click();
    await page.getByRole('option', { name: 'No template - plain look' }).click();
    // The select hands the focus back to its trigger once its list has closed;
    // wait for that so it cannot land in the middle of the key sequence below.
    await expect(templatePick).toBeFocused();
    const { dark, light } = themeRadios(dialog);
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    await expect(dark).toBeEnabled();
    await expect(light).toBeEnabled();

    // The checked option is the group's only tab stop.
    await expect(dark).toHaveAttribute('tabindex', '0');
    await expect(light).toHaveAttribute('tabindex', '-1');

    // An arrow moves the focus AND the selection; the tab stop follows.
    await dark.focus();
    await page.keyboard.press('ArrowRight');
    await expect(light).toBeFocused();
    await expect(light).toHaveAttribute('aria-checked', 'true');
    await expect(dark).toHaveAttribute('aria-checked', 'false');
    await expect(light).toHaveAttribute('tabindex', '0');
    await expect(dark).toHaveAttribute('tabindex', '-1');

    // Wrapping, both ways and along both axes.
    await page.keyboard.press('ArrowRight');
    await expect(dark).toBeFocused();
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    await page.keyboard.press('ArrowUp');
    await expect(light).toBeFocused();
    await expect(light).toHaveAttribute('aria-checked', 'true');
    await page.keyboard.press('ArrowDown');
    await expect(dark).toBeFocused();
    await page.keyboard.press('ArrowLeft');
    await expect(light).toBeFocused();

    // Home and End jump to the ends.
    await page.keyboard.press('Home');
    await expect(dark).toBeFocused();
    await expect(dark).toHaveAttribute('aria-checked', 'true');
    await page.keyboard.press('End');
    await expect(light).toBeFocused();
    await expect(light).toHaveAttribute('aria-checked', 'true');

    // Tab leaves the group in one step, and Shift+Tab returns to the checked
    // option - never to the other one.
    await page.keyboard.press('Tab');
    await expect(dialog.getByLabel('About (optional)')).toBeFocused();
    await page.keyboard.press('Shift+Tab');
    await expect(light).toBeFocused();
  });

  test('should skip a scheme the template cannot dress and keep it out of the tab order', async ({ page }) => {
    const template = await findSingleSchemePresentationTemplate();
    test.skip(!template, 'No presentation template supporting a single theme on this platform');
    if (!template) return;

    await page.goto(`/templates/${template.id}`);
    const dialog = await openNewPresentation(
      page,
      page.getByRole('button', { name: /^New presentation with this template/ }),
    );
    const { group, dark, light } = themeRadios(dialog);
    const [enabled, disabled] = (await dark.isEnabled()) ? [dark, light] : [light, dark];

    // The unsupported scheme is disabled, says why to assistive tech, and its
    // hoverable tooltip wrapper is not a tab stop of its own.
    await expect(disabled).toBeDisabled();
    const name = template.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    await expect(disabled).toHaveAccessibleDescription(new RegExp(`Not supported by ${name}`));
    await expect(group.locator('span[aria-disabled="true"]')).toHaveAttribute('tabindex', '-1');
    await expect(enabled).toHaveAttribute('aria-checked', 'true');
    await expect(enabled).toHaveAttribute('tabindex', '0');

    // The keys skip it: with one option left, every move stays put.
    await enabled.focus();
    for (const key of ['ArrowRight', 'ArrowLeft', 'End', 'Home']) {
      await page.keyboard.press(key);
      await expect(enabled).toBeFocused();
      await expect(enabled).toHaveAttribute('aria-checked', 'true');
    }
    await expect(disabled).toHaveAttribute('aria-checked', 'false');

    // Tab leaves the group straight for the next field.
    await page.keyboard.press('Tab');
    await expect(dialog.getByLabel('About (optional)')).toBeFocused();
  });
});
