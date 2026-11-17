import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { createAdminApiContext, expectEditorSaved } from './helpers';

// ---------------------------------------------------------------------------
// Presentations - in-place manual editing in the viewer, saved on its
// own and merged with a collaborator's changes.
//
// The editor panel (`?panel=editor`) turns the stage into a slide editor: a
// click selects a block (outline + floating bar), a click on a text opens it
// in place, the keyboard arranges (Alt+arrows, Delete, Ctrl+D, Ctrl+Z), the
// palette inserts - and every change saves itself, the words typed in place
// included, into ONE history entry per person and session. A revision a
// collaborator saves meanwhile is merged into the draft instead of asking for
// a reload. Real input events throughout - a contentEditable and
// document-level key handling are exactly what a synthetic `dispatchEvent`
// would not prove.
//
// Every test imports its own deck through the API from a bare spec (no LLM
// call), owned by the admin and deleted afterwards: every edit is saved now.
// ---------------------------------------------------------------------------

const FIXTURE_SPEC = {
  title: 'E2E editor deck',
  theme: 'dark',
  aspect_ratio: '16:9',
  slides: [
    { layout: 'title', title: 'E2E editor', subtitle: 'Select, type, arrange' },
    {
      layout: 'content',
      title: 'What changes',
      blocks: [
        { type: 'heading', content: 'A heading to select', level: 3 },
        {
          type: 'list',
          items: [{ text: 'First point' }, { text: 'Second point' }, { text: 'Third point' }],
        },
        { type: 'text', content: 'A paragraph under the list.' },
      ],
    },
    { layout: 'closing', title: 'Thank you' },
  ],
};

interface StoredDeck {
  revision: number;
  spec: {
    slides: { title?: string; blocks?: { type: string; content?: string; items?: { text: string }[] }[] }[];
  };
}

async function importFixtureDeck(apiCtx: APIRequestContext, token: string): Promise<string> {
  const resp = await apiCtx.post('/api/v1/presentations/import', {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      file: {
        name: 'e2e-editor.json',
        mimeType: 'application/json',
        buffer: Buffer.from(JSON.stringify(FIXTURE_SPEC), 'utf8'),
      },
    },
  });
  if (!resp.ok()) throw new Error(`Importing the fixture deck failed: ${resp.status()} ${await resp.text()}`);
  const { id } = (await resp.json()) as { id: string };
  return id;
}

/** The deck as the server stores it now. */
async function storedDeck(deckId: string): Promise<StoredDeck> {
  const { apiCtx, token } = await createAdminApiContext();
  try {
    const deck = await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    expect(deck.ok()).toBeTruthy();
    return (await deck.json()) as StoredDeck;
  } finally {
    await apiCtx.dispose();
  }
}

/** The viewer's counter button reads `2 / 3` and is named "Slide 2 of 3. Go to slide". */
async function expectStageSlide(page: Page, current: number): Promise<void> {
  await expect(page.getByRole('button', { name: new RegExp(`^Slide ${current} of \\d+\\.`) })).toBeVisible();
}

/** The slide on the stage, in edit mode, positioned by its zero-based index. */
function stageSlide(page: Page, index: number) {
  return page.locator(`.reveal .deck-slide[data-slide-index="${index}"]`);
}

/**
 * The visible toasts. Radix mirrors every toast into an off-screen
 * `aria-live` region, so the list is read from the viewport, the only
 * `<ol tabindex="-1">` on the page.
 */
function toasts(page: Page) {
  return page.locator('ol[tabindex="-1"] > li');
}

/**
 * Closes every open toast instead of waiting out their 4 s, so the next count
 * starts from none. One click per toast at most: one that does not close is
 * left to its own 4 s.
 */
async function dismissAllToasts(page: Page): Promise<void> {
  const closes = page
    .locator('ol[tabindex="-1"] > li[data-state="open"]')
    .getByRole('button', { name: 'Close notification' });
  for (const close of await closes.all()) await close.click({ timeout: 1_000 }).catch(() => undefined);
  await expect(toasts(page)).toHaveCount(0);
}

/** The in-place field's text, exactly (`toHaveText` would collapse its spaces). */
async function fieldText(page: Page): Promise<string> {
  return page.locator('[data-edit-editing="true"]').evaluate((element) => element.textContent ?? '');
}

/**
 * In the in-place field (one line of plain text): select `[from, to)`, and
 * return the screen point of the caret boundary `at` - on the left edge of the
 * character after it, or the right edge of the last character at the end.
 */
async function fieldRange(page: Page, from: number, to: number, at: number): Promise<{ x: number; y: number }> {
  return page.locator('[data-edit-editing="true"]').evaluate(
    (element, [start, end, point]) => {
      // The text node and local offset of a boundary (`inclusive`: the end of
      // a node, as a selection reads it) or of the character starting there.
      const locate = (offset: number, inclusive: boolean): { node: Text; offset: number } => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        let remaining = offset;
        for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
          if (inclusive ? remaining <= node.length : remaining < node.length) return { node, offset: remaining };
          remaining -= node.length;
        }
        throw new Error(`No offset ${offset} in the field`);
      };
      const range = document.createRange();
      const a = locate(start, true);
      const b = locate(end, true);
      range.setStart(a.node, a.offset);
      range.setEnd(b.node, b.offset);
      const selection = window.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      const atEnd = point >= (element.textContent?.length ?? 0);
      const c = locate(atEnd ? point - 1 : point, false);
      const glyph = document.createRange();
      glyph.setStart(c.node, c.offset);
      glyph.setEnd(c.node, c.offset + 1);
      const box = glyph.getBoundingClientRect();
      return { x: atEnd ? box.right - 1 : box.left + 1, y: box.top + box.height / 2 };
    },
    [from, to, at],
  );
}

/** Opens the viewer with the editor panel and lands on slide 2 (the content slide). */
async function openEditor(page: Page, deckId: string): Promise<void> {
  await page.goto(`/presentations/${deckId}?panel=editor`);
  await expect(page.locator('.reveal.ready')).toBeVisible();
  await expect(page.getByTestId('presentation.editorPanel')).toBeVisible();
  // Edit mode is on: the renderer publishes the block paths.
  await expect(page.locator('[data-deck-editing="true"]')).toBeVisible();
  await page.getByRole('button', { name: 'Next slide' }).click();
  await expectStageSlide(page, 2);
}

/**
 * Holds the page's reads of the deck until the test lets them through: the
 * presence beat and the poll must not bring a collaborator's revision in
 * before the page has made its own change on the revision it has.
 */
async function holdDeckReads(page: Page, deckId: string): Promise<{ release: () => void; patches: number[] }> {
  let release: () => void = () => undefined;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const patches: number[] = [];
  await page.route(`**/api/v1/presentations/${deckId}`, async (route) => {
    const method = route.request().method();
    if (method === 'GET') await held;
    if (method === 'PATCH') {
      const response = await route.fetch();
      patches.push(response.status());
      await route.fulfill({ response });
      return;
    }
    await route.continue();
  });
  return { release, patches };
}

/** A collaborator (an API client) saves the deck's slide `index` with a new title. */
async function collaboratorRetitles(deckId: string, index: number, title: string): Promise<number> {
  const { apiCtx, token } = await createAdminApiContext();
  const headers = { Authorization: `Bearer ${token}` };
  try {
    const current = (await (await apiCtx.get(`/api/v1/presentations/${deckId}`, { headers })).json()) as {
      revision: number;
      spec: { slides: Record<string, unknown>[] };
    };
    const slides = current.spec.slides.map((s, at) => (at === index ? { ...s, title } : s));
    const theirs = await apiCtx.patch(`/api/v1/presentations/${deckId}`, {
      headers,
      data: { slides, expected_revision: current.revision },
    });
    expect(theirs.ok()).toBeTruthy();
    return current.revision + 1;
  } finally {
    await apiCtx.dispose();
  }
}

test.describe('Presentations - in-place editing', () => {
  let deckId = '';

  test.beforeEach(async () => {
    const { apiCtx, token } = await createAdminApiContext();
    try {
      deckId = await importFixtureDeck(apiCtx, token);
    } finally {
      await apiCtx.dispose();
    }
  });

  test.afterEach(async () => {
    if (!deckId) return;
    const { apiCtx, token } = await createAdminApiContext();
    try {
      await apiCtx.delete(`/api/v1/presentations/${deckId}`, { headers: { Authorization: `Bearer ${token}` } });
    } finally {
      await apiCtx.dispose();
    }
  });

  test('should select, type in place, arrange with the keyboard and save it all on its own, in one history entry', async ({
    page,
  }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const heading = slide.locator('[data-edit-block="blocks.0"]');
    const list = slide.locator('[data-edit-block="blocks.1"]');

    // A click on the heading's empty right-hand side selects the BLOCK (no
    // glyph under the pointer): outline, floating bar, inspector on Selection.
    const headingBox = await heading.boundingBox();
    if (!headingBox) throw new Error('The heading has no box');
    await heading.click({ position: { x: headingBox.width - 8, y: headingBox.height / 2 } });
    await expect(heading).toHaveAttribute('data-edit-selected', 'true');
    await expect(page.getByTestId('presentation.blockBar')).toBeVisible();
    await expect(page.getByTestId('presentation.blockBar')).toContainText('Heading');
    await expect(page.getByTestId('presentation.editorTab.selection')).toHaveAttribute('aria-selected', 'true');

    // Enter opens the selected block's main text in place; Escape leaves it
    // untouched and keeps the block selected - nothing to save.
    await page.keyboard.press('Enter');
    const editing = page.locator('[data-edit-editing="true"]');
    await expect(editing).toBeVisible();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.0.content');
    await page.keyboard.press('Escape');
    await expect(editing).toBeHidden();
    await expect(heading).toHaveAttribute('data-edit-selected', 'true');
    await expectEditorSaved(page);

    // A click ON a text opens it at once, caret where the pointer is. Typing
    // at the end and Ctrl+Enter closes the bullet: the slide shows the new
    // words, the text bar was there while typing.
    const bullet = slide.locator('[data-edit-text="blocks.1.items.1.text"]');
    await bullet.click();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.1.items.1.text');
    await expect(page.getByTestId('presentation.textBar')).toBeVisible();
    await page.keyboard.press('End');
    await page.keyboard.type(' edited in place');
    await page.keyboard.press('Control+Enter');
    await expect(editing).toBeHidden();
    await expect(slide.locator('[data-edit-text="blocks.1.items.1.text"]')).toHaveText('Second point edited in place');

    // The list is the selected block now (the text lived in it). Alt+Up moves
    // it above the heading; Ctrl+Z puts it back: one undo step per change.
    await expect(list).toHaveAttribute('data-edit-selected', 'true');
    await page.keyboard.press('Alt+ArrowUp');
    await expect(slide.locator('[data-edit-block="blocks.0"]')).toHaveClass(/deck-list/);
    await expect(slide.locator('[data-edit-block="blocks.0"]')).toHaveAttribute('data-edit-selected', 'true');
    await page.keyboard.press('Control+z');
    await expect(slide.locator('[data-edit-block="blocks.0"]')).toHaveClass(/deck-heading/);
    await expect(slide.locator('[data-edit-block="blocks.1"]')).toHaveAttribute('data-edit-selected', 'true');

    // The palette on the block bar inserts after the selection: a new quote
    // block at `blocks.2`, selected, pushing the paragraph to `blocks.3`.
    await page.getByTestId('presentation.blockBar').getByRole('button', { name: 'Insert a block after this one' }).click();
    await page.getByRole('menuitem', { name: 'Quote' }).click();
    await expect(slide.locator('[data-edit-block="blocks.2"]')).toHaveAttribute('data-edit-selected', 'true');
    await expect(slide.locator('[data-edit-block="blocks.2"]')).toHaveClass(/deck-quote/);
    await expect(slide.locator('[data-edit-block="blocks.3"]')).toHaveClass(/deck-text/);

    // Delete removes the selected block; the paragraph is back at `blocks.2`.
    await page.keyboard.press('Delete');
    await expect(slide.locator('[data-edit-block="blocks.2"]')).toHaveClass(/deck-text/);
    await expect(slide.locator('[data-edit-block="blocks.3"]')).toHaveCount(0);

    // No Save to click: it is all on the server once the status line says so,
    // and the history holds ONE entry for the whole session, named after the
    // slide it touched - not one per autosave.
    await expectEditorSaved(page);
    await expect(page.getByTestId('presentation.save')).toHaveCount(0);
    const body = await storedDeck(deckId);
    const blocks = body.spec.slides[1].blocks ?? [];
    expect(blocks.map((block) => block.type)).toEqual(['heading', 'list', 'text']);
    expect(blocks[1].items?.[1].text).toBe('Second point edited in place');
    const { apiCtx, token } = await createAdminApiContext();
    try {
      const versions = await apiCtx.get(`/api/v1/presentations/${deckId}/versions`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      expect(versions.ok()).toBeTruthy();
      const manual = ((await versions.json()) as { revision: number; source: string; summary: string | null }[]).filter(
        (version) => version.source === 'manual',
      );
      expect(manual).toHaveLength(1);
      expect(manual[0].revision).toBe(body.revision);
      expect(manual[0].summary).toBe('Edited slide 2');
    } finally {
      await apiCtx.dispose();
    }

    // The saved deck survives a reload, the words on the slide.
    await page.reload();
    await expect(page.locator('.reveal.ready')).toBeVisible();
    await expect(stageSlide(page, 1).locator('[data-edit-text="blocks.1.items.1.text"]')).toHaveText(
      'Second point edited in place',
    );
  });

  test('should save the words typed in place while the field is still open, and take them back on Escape', async ({
    page,
  }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const editing = page.locator('[data-edit-editing="true"]');
    const thirdOnServer = async () => (await storedDeck(deckId)).spec.slides[1].blocks?.[1].items?.[2].text;

    // Words typed and never committed by hand reach the server all the same:
    // the field hands them to the draft as they are typed.
    const bullet = slide.locator('[data-edit-text="blocks.1.items.2.text"]');
    await bullet.click();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.1.items.2.text');
    await page.keyboard.press('End');
    await page.keyboard.type(' kept while typing');
    await expect.poll(thirdOnServer, { timeout: 15_000 }).toBe('Third point kept while typing');
    await expect(editing).toBeVisible();

    // Ctrl+S saves now and leaves the field open.
    await page.keyboard.type(' and more');
    await page.keyboard.press('Control+s');
    await expect(editing).toBeVisible();
    await expect.poll(thirdOnServer, { timeout: 15_000 }).toBe('Third point kept while typing and more');

    // Escape takes back what the field typed - on the server too.
    await page.keyboard.press('Escape');
    await expect(editing).toBeHidden();
    await expect(slide.locator('[data-edit-text="blocks.1.items.2.text"]')).toHaveText('Third point');
    await expect.poll(thirdOnServer, { timeout: 15_000 }).toBe('Third point');
  });

  test('should land on the slide it adds, save it on its own and leave the editor without asking', async ({ page }) => {
    await openEditor(page, deckId);

    // "Add slide" inserts after the current slide and moves the stage onto
    // the new slide (not the one that used to hold its index).
    await page.getByTestId('presentation.editorPanel').getByRole('button', { name: 'Add a slide' }).click();
    await page.getByRole('menuitem', { name: 'Section' }).click();
    await expectStageSlide(page, 3);
    // The section on screen IS the new slide (reveal marks the current section `present`).
    await expect(page.locator('.reveal section.present .deck-slide')).toHaveAttribute('data-layout', 'section');
    await expect(page.locator('.reveal section.present .deck-slide')).toHaveAttribute('data-slide-index', '2');
    await expectEditorSaved(page);
    expect((await storedDeck(deckId)).spec.slides).toHaveLength(4);

    // Closing the panel over saved work asks nothing.
    await page.getByTestId('presentation.editorPanel').getByRole('button', { name: 'Close' }).click();
    await expect(page.getByTestId('presentation.editorPanel')).toBeHidden();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /^Slide \d+ of 4\./ })).toBeVisible();

    // Out of edit mode the deck renders the DOM it always did: not one edit
    // attribute, not the layer - what a presented deck, a chat card and the
    // export page get (byte-identical outside the editor is the contract).
    await expect(page.locator('[data-deck-editing]')).toHaveCount(0);
    await expect(page.getByTestId('presentation.editLayer')).toHaveCount(0);
    await expect(
      page.locator('.reveal [data-edit-block], .reveal [data-edit-text], .reveal [data-slide-index], .reveal [data-edit-selected]'),
    ).toHaveCount(0);
  });

  test('should undo and redo from the panel fields, and across the autosaves', async ({ page }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const panel = page.getByTestId('presentation.editorPanel');
    const titleOnServer = async () => (await storedDeck(deckId)).spec.slides[1].title;

    // A change typed in a panel FIELD is undone by Ctrl+Z with the focus still
    // in that field: the deck's undo is the field's undo (the browser's own,
    // which knows nothing of the draft, is suppressed).
    await panel.getByRole('tab', { name: 'Selection' }).click();
    // The slide's own title field (`deck-field-<slide>-<path>`), not a block's.
    const title = panel.locator('#deck-field-1-title');
    await title.click();
    await page.keyboard.press('End');
    await page.keyboard.type(' today');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes today');
    await page.keyboard.press('Control+z');
    await expect(title).toHaveValue('What changes');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes');
    await page.keyboard.press('Control+y');
    await expect(title).toHaveValue('What changes today');

    // The browser's own undo asked WITHOUT a key - the Edit menu, the field's
    // context menu - reaches the field as a `beforeinput` of type
    // `historyUndo`. It is routed to the session like the key: the draft and the field follow the deck's history, and the
    // input's native history is cancelled (`defaultPrevented`). A synthetic
    // event has no default action of its own, so what the flag proves is the
    // routing.
    const menuHistory = (inputType: 'historyUndo' | 'historyRedo') =>
      title.evaluate((element, type) => {
        const event = new InputEvent('beforeinput', { inputType: type, bubbles: true, cancelable: true });
        element.dispatchEvent(event);
        return event.defaultPrevented;
      }, inputType);
    expect(await menuHistory('historyUndo')).toBe(true);
    await expect(title).toHaveValue('What changes');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes');
    expect(await menuHistory('historyRedo')).toBe(true);
    await expect(title).toHaveValue('What changes today');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes today');

    // The autosave is a checkpoint, not the end of the history: Ctrl+Z after
    // it takes the deck back to before the rename, and that is saved too.
    await expect.poll(titleOnServer, { timeout: 15_000 }).toBe('What changes today');
    await expectEditorSaved(page);
    await expect(page.getByTestId('presentation.undo')).toBeEnabled();
    await page.getByTestId('presentation.undo').focus();
    await page.keyboard.press('Control+z');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes');
    await expect.poll(titleOnServer, { timeout: 15_000 }).toBe('What changes');
    // ...and the redo stack survived both saves.
    await expect(page.getByTestId('presentation.redo')).toBeEnabled();
    await page.getByTestId('presentation.undo').focus();
    await page.keyboard.press('Control+y');
    await expect(slide.locator('[data-edit-text="title"]')).toHaveText('What changes today');
    await expect.poll(titleOnServer, { timeout: 15_000 }).toBe('What changes today');
  });

  test('should walk a text session with Ctrl+Z / Ctrl+Y, formatting included', async ({ page }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const editing = page.locator('[data-edit-editing="true"]');

    // Type, then bold the word: two steps of the field's own history.
    const paragraph = slide.locator('[data-edit-text="blocks.2.content"]');
    await paragraph.click();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.2.content');
    await page.keyboard.press('End');
    await page.keyboard.type(' Remember');
    await page.keyboard.press('Shift+Control+ArrowLeft');
    await page.keyboard.press('Control+b');
    await expect(editing.locator('strong')).toHaveText('Remember');

    // The browser's own undo asked without a key (the Edit menu, the context
    // menu) reaches the field as a `beforeinput` of type `historyUndo`: the
    // FIELD's own history walks it - the layer leaves a text under edit alone -
    // so the bold goes and comes back.
    const menuHistoryInPlace = (inputType: 'historyUndo' | 'historyRedo') =>
      editing.evaluate((element, type) => {
        const event = new InputEvent('beforeinput', { inputType: type, bubbles: true, cancelable: true });
        element.dispatchEvent(event);
        return event.defaultPrevented;
      }, inputType);
    expect(await menuHistoryInPlace('historyUndo')).toBe(true);
    await expect(editing.locator('strong')).toHaveCount(0);
    expect(await menuHistoryInPlace('historyRedo')).toBe(true);
    await expect(editing.locator('strong')).toHaveText('Remember');

    // Ctrl+Z undoes the formatting (the browser's own undo could not: the
    // field was rebuilt) and leaves the word it styled selected; a second one
    // the typing; Ctrl+Y brings the typing back. The field stays open
    // throughout.
    await page.keyboard.press('Control+z');
    await expect(editing.locator('strong')).toHaveCount(0);
    await expect(editing).toHaveText('A paragraph under the list. Remember');
    await expect
      .poll(() => page.evaluate(() => window.getSelection()?.toString() ?? ''))
      .toBe('Remember');
    await page.keyboard.press('Control+z');
    await expect(editing).toHaveText('A paragraph under the list.');
    await page.keyboard.press('Control+y');
    await expect(editing).toHaveText('A paragraph under the list. Remember');

    // Typing somewhere else within the window is a step of its own: Ctrl+Z
    // takes back the words typed at the start, not the ones at the end too,
    // and puts the caret back where that typing began.
    await page.keyboard.press('Home');
    await page.keyboard.type('Now: ');
    await expect(editing).toHaveText('Now: A paragraph under the list. Remember');
    await page.keyboard.press('Control+z');
    await expect(editing).toHaveText('A paragraph under the list. Remember');
    await expect
      .poll(() => page.evaluate(() => window.getSelection()?.getRangeAt(0).startOffset ?? -1))
      .toBe(0);

    // Escape drops the whole edit - what the field had handed to the draft
    // meanwhile included: the paragraph as stored, on the server too.
    await page.keyboard.press('Escape');
    await expect(editing).toBeHidden();
    await expect(paragraph).toHaveText('A paragraph under the list.');
    await expect
      .poll(async () => (await storedDeck(deckId)).spec.slides[1].blocks?.[2].content, { timeout: 15_000 })
      .toBe('A paragraph under the list.');
  });

  test('should keep a burst of typing to one slide: the same field on the next slide is another step', async ({
    page,
  }) => {
    // `Date.now()` is frozen for the whole test (timers keep running): every
    // keystroke below falls inside the coalescing window, so what tells the
    // two bursts apart is the field they went to - the same path, `title`, on
    // two slides - never the clock.
    await page.clock.setFixedTime(new Date('2026-09-25T10:00:00Z'));
    await openEditor(page, deckId);
    const panel = page.getByTestId('presentation.editorPanel');
    const contentTitle = stageSlide(page, 1).locator('[data-edit-text="title"]');
    const closingTitle = stageSlide(page, 2).locator('[data-edit-text="title"]');

    await panel.getByRole('tab', { name: 'Selection' }).click();
    await panel.locator('#deck-field-1-title').click();
    await page.keyboard.press('End');
    await page.keyboard.type(' alpha');
    await expect(contentTitle).toHaveText('What changes alpha');

    // The stage moves to the closing slide: the selection is dropped with it
    // and the inspector shows that slide's fields - the same `title` path.
    await page.getByRole('button', { name: 'Next slide' }).click();
    await expectStageSlide(page, 3);
    await panel.locator('#deck-field-2-title').click();
    await page.keyboard.press('End');
    await page.keyboard.type(' beta');
    await expect(closingTitle).toHaveText('Thank you beta');

    // Two fields, two steps: the first Ctrl+Z takes back the closing slide's
    // words only, the second the content slide's.
    await page.keyboard.press('Control+z');
    await expect(closingTitle).toHaveText('Thank you');
    await expect(contentTitle).toHaveText('What changes alpha');
    await page.keyboard.press('Control+z');
    await expect(contentTitle).toHaveText('What changes');
    await expectEditorSaved(page);
  });

  test('should keep an in-place text open when its markup would be refused, in the inspector words', async ({
    page,
  }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const panel = page.getByTestId('presentation.editorPanel');
    const editing = page.locator('[data-edit-editing="true"]');
    const status = page.getByTestId('presentation.saveStatus');
    const paragraph = slide.locator('[data-edit-text="blocks.2.content"]');
    const paragraphOnServer = async () => (await storedDeck(deckId)).spec.slides[1].blocks?.[2].content ?? '';
    // What the inspector says under a field holding the same text - the
    // field's own refusals, never the autosave's report of a refused save.
    const refusal = 'Unbalanced `: every ** and ` must be closed, and [[tone:text]] needs both brackets.';
    const refusals = toasts(page).filter({ hasText: refusal }).filter({ hasNotText: 'Your last change could not be saved' });
    const stored = 'A paragraph under the list.';
    const typed = `${stored} Wrap it in a \` please`;

    // The grammar has no escape: a lone backtick is markup the save refuses.
    await paragraph.click();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.2.content');
    await page.keyboard.press('End');
    await page.keyboard.type(' Wrap it in a ` please');

    // Done is refused - Ctrl+Enter, Tab, Ctrl+S alike: the field stays open
    // with the text as typed, one toast per attempt says why, and the refused
    // markup reaches neither the draft nor the server.
    await page.keyboard.press('Control+Enter');
    await expect(refusals).toHaveCount(1);
    await expect(editing).toBeVisible();
    // A held key auto-repeats: the repeats ask nothing more, so a refused
    // text raises no toast per repeat (Playwright never auto-repeats, hence
    // the synthetic repeats - read two frames later, once anything they
    // scheduled has rendered).
    await editing.evaluate(async (element) => {
      for (const init of [{ key: 'Enter', ctrlKey: true }, { key: 'Tab' }, { key: 's', ctrlKey: true }]) {
        element.dispatchEvent(new KeyboardEvent('keydown', { ...init, repeat: true, bubbles: true, cancelable: true }));
      }
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    });
    expect(await refusals.count()).toBe(1);
    await expect(editing).toBeVisible();
    await page.keyboard.press('Tab');
    await expect(refusals).toHaveCount(2);
    await page.keyboard.press('Control+s');
    await expect(refusals).toHaveCount(3);
    expect(await fieldText(page)).toBe(typed);
    expect(await paragraphOnServer()).not.toContain('`');

    // Escape still cancels: the paragraph as stored.
    await page.keyboard.press('Escape');
    await expect(editing).toBeHidden();
    await expect(paragraph).toHaveText(stored);
    await expect.poll(paragraphOnServer, { timeout: 15_000 }).toBe(stored);
    await dismissAllToasts(page);

    // A blur keeps the words - committed as typed, flagged the same way - and
    // the inspector says it again under the field, where it can be fixed. The
    // server refuses that draft, and the status line says the change could
    // not be saved.
    await paragraph.click();
    await page.keyboard.press('End');
    await page.keyboard.type(' Wrap it in a ` please');
    const field = panel.locator('#deck-field-1-blocks-2-content');
    await field.click();
    await expect(editing).toBeHidden();
    await expect(refusals).toHaveCount(1);
    await expect(field).toHaveValue(typed);
    await expect(panel.getByText(refusal)).toBeVisible();
    await expect(status).toHaveAttribute('data-state', 'invalid', { timeout: 15_000 });
    await dismissAllToasts(page);

    // Ctrl+S with the text open again: its hand-over is refused - one toast,
    // no save, and the field keeps the focus to be fixed.
    await paragraph.click();
    await expect(editing).toBeVisible();
    await page.keyboard.press('End');
    await page.keyboard.type(' now');
    await page.keyboard.press('Control+s');
    await expect(refusals).toHaveCount(1);
    await expect(editing).toBeVisible();
    await page.keyboard.type('!');
    expect(await fieldText(page)).toBe(`${typed} now!`);

    // Fixed in place, the text commits - back to the paragraph as stored, and
    // nothing is left to save.
    await page.keyboard.press('Control+a');
    await page.keyboard.type(stored);
    await page.keyboard.press('Control+Enter');
    await expect(editing).toBeHidden();
    await expect(paragraph).toHaveText(stored);
    await expect(panel.getByText(refusal)).toHaveCount(0);
    await expectEditorSaved(page);
    expect(await paragraphOnServer()).toBe(stored);
  });

  test('should drop plain text like a paste, and move text dragged inside the field once', async ({ page }) => {
    await openEditor(page, deckId);
    const slide = stageSlide(page, 1);
    const editing = page.locator('[data-edit-editing="true"]');
    const paragraph = slide.locator('[data-edit-text="blocks.2.content"]');
    const stored = 'A paragraph under the list.';

    await paragraph.click();
    await expect(editing).toHaveAttribute('data-edit-text', 'blocks.2.content');

    // A drop of rich content - what a web page or Word puts in a drag: the
    // browser's own drop would insert that HTML, the field inserts the plain
    // text at the drop point, like a paste (the caret sits at the start: the
    // drop point decides, not the caret). The drop point is measured in the
    // same task that dispatches the drop: the stage may still re-lay out the
    // field it just opened, and a point measured one round trip earlier then
    // lands mid-sentence ("A paragraph under Dropped words the list.").
    const prevented = await editing.evaluate((element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let last: Text | null = null;
      for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
        if (node.length > 0) last = node;
      }
      if (!last) throw new Error('No text in the field');
      const caret = document.createRange();
      const first = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode() ?? last;
      caret.setStart(first, 0);
      caret.collapse(true);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(caret);
      const glyph = document.createRange();
      glyph.setStart(last, last.length - 1);
      glyph.setEnd(last, last.length);
      const box = glyph.getBoundingClientRect();
      const point = { x: box.right - 1, y: box.top + box.height / 2 };
      const data = new DataTransfer();
      data.setData('text/html', '<b style="color: red">Dropped</b> <font face="Comic Sans MS">words</font>');
      data.setData('text/plain', ' Dropped words');
      const drop = new DragEvent('drop', {
        bubbles: true,
        cancelable: true,
        dataTransfer: data,
        clientX: point.x,
        clientY: point.y,
      });
      element.dispatchEvent(drop);
      return drop.defaultPrevented;
    });
    expect(prevented).toBe(true);
    await expect.poll(() => fieldText(page)).toBe(`${stored} Dropped words`);
    await expect(editing.locator('b, font, [style]')).toHaveCount(0);
    // The drop reaches the draft and is saved, the field still open - and the
    // stage re-laid out around it - before the next gesture, a native drag
    // whose grab point is measured on screen.
    const paragraphOnServer = async () => (await storedDeck(deckId)).spec.slides[1].blocks?.[2].content ?? '';
    await expect.poll(paragraphOnServer, { timeout: 15_000 }).toBe(`${stored} Dropped words`);
    await expectEditorSaved(page);

    // A drag of the field's own text is a move: `Dropped` leaves its place
    // and lands at the start, once - never duplicated, never lost.
    const word = `${stored} `.length;
    const grab = await fieldRange(page, word, word + 'Dropped'.length, word + 2);
    const start = await fieldRange(page, word, word + 'Dropped'.length, 0);
    await page.mouse.move(grab.x, grab.y);
    await page.mouse.down();
    await page.mouse.move(start.x, start.y, { steps: 12 });
    await page.mouse.up();
    await expect.poll(() => fieldText(page)).toBe(`Dropped${stored}  words`);

    // Each drop is one step of the field's own history.
    await page.keyboard.press('Control+z');
    await expect.poll(() => fieldText(page)).toBe(`${stored} Dropped words`);
    await page.keyboard.press('Control+z');
    await expect.poll(() => fieldText(page)).toBe(stored);

    // Committed, the text the slide keeps is what was shown: plain words.
    await page.keyboard.press('Control+y');
    await page.keyboard.press('Control+Enter');
    await expect(editing).toBeHidden();
    await expect(paragraph).toHaveText(`${stored} Dropped words`);
    await expect(paragraph.locator('b, font, [style]')).toHaveCount(0);
  });

  test("should merge a collaborator's change into the draft, both kept, with no reload asked", async ({
    page,
  }) => {
    await openEditor(page, deckId);
    const reads = await holdDeckReads(page, deckId);
    const panel = page.getByTestId('presentation.editorPanel');
    const contentTitle = stageSlide(page, 1).locator('[data-edit-text="title"]');
    const closingTitle = stageSlide(page, 2).locator('[data-edit-text="title"]');
    try {
      // A collaborator saves the closing slide while this page still holds
      // the revision before it (its reads are held)...
      await collaboratorRetitles(deckId, 2, 'Thank you, from a collaborator');

      // ...and a change typed here goes out on that older revision: refused
      // with a 409, the draft kept.
      await panel.getByRole('tab', { name: 'Selection' }).click();
      await panel.locator('#deck-field-1-title').click();
      await page.keyboard.press('End');
      await page.keyboard.type(' (mine)');
      await expect.poll(() => reads.patches, { timeout: 15_000 }).toContain(409);
      await expect(contentTitle).toHaveText('What changes (mine)');
      // The filmstrip is the Slides tab: on screen when their change lands.
      await panel.getByRole('tab', { name: 'Slides' }).click();

      // Their revision comes in: merged under the draft, nothing asked - their
      // slide flashes in the filmstrip - and the change is saved on top of it.
      reads.release();
      await expect(closingTitle).toHaveText('Thank you, from a collaborator');
      await expect(contentTitle).toHaveText('What changes (mine)');
      await expect(page.getByTestId('presentation.slideRow.2')).toHaveAttribute('data-remote-change', 'true');
      await expect(page.getByRole('dialog')).toHaveCount(0);
      await expectEditorSaved(page);
      const stored = await storedDeck(deckId);
      expect(stored.spec.slides[1].title).toBe('What changes (mine)');
      expect(stored.spec.slides[2].title).toBe('Thank you, from a collaborator');
      expect(reads.patches.at(-1)).toBe(200);
      // Undo takes back this page's change only, never theirs.
      await page.getByTestId('presentation.undo').click();
      await expect(contentTitle).toHaveText('What changes');
      await expect(closingTitle).toHaveText('Thank you, from a collaborator');
    } finally {
      reads.release();
    }
  });

  test('should keep my version and say so when a collaborator changed the same field', async ({ page }) => {
    await openEditor(page, deckId);
    const reads = await holdDeckReads(page, deckId);
    const panel = page.getByTestId('presentation.editorPanel');
    const contentTitle = stageSlide(page, 1).locator('[data-edit-text="title"]');
    try {
      await collaboratorRetitles(deckId, 1, 'From a collaborator');
      await panel.getByRole('tab', { name: 'Selection' }).click();
      await panel.locator('#deck-field-1-title').click();
      await page.keyboard.press('End');
      await page.keyboard.type(' (mine)');
      await expect.poll(() => reads.patches, { timeout: 15_000 }).toContain(409);

      // Both changed the slide's title: this page saves last, so its version
      // stays - and the person is told on which slide.
      reads.release();
      await expect(toasts(page).filter({ hasText: /changed the same thing as you/ })).toHaveCount(1);
      await expect(toasts(page).filter({ hasText: 'Your version was kept on slide 2.' })).toHaveCount(1);
      await expect(contentTitle).toHaveText('What changes (mine)');
      await expectEditorSaved(page);
      expect((await storedDeck(deckId)).spec.slides[1].title).toBe('What changes (mine)');
    } finally {
      reads.release();
    }
  });

  test('should leave the editor without asking once saved, and ask only when the last save failed', async ({
    page,
  }) => {
    await openEditor(page, deckId);
    const panel = page.getByTestId('presentation.editorPanel');
    const close = panel.getByRole('button', { name: 'Close', exact: true });
    const contentTitle = stageSlide(page, 1).locator('[data-edit-text="title"]');

    // Saved: the editor closes at once, nothing asked.
    await panel.getByRole('tab', { name: 'Selection' }).click();
    await panel.locator('#deck-field-1-title').click();
    await page.keyboard.press('End');
    await page.keyboard.type(' (saved)');
    await close.click();
    await expect(panel).toBeHidden();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect((await storedDeck(deckId)).spec.slides[1].title).toBe('What changes (saved)');

    // A save the server refuses: the status line says so, and leaving asks.
    await page.route(`**/api/v1/presentations/${deckId}`, async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.continue();
        return;
      }
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Slide 2 (What changes): refused for the test' }),
      });
    });
    await openEditor(page, deckId);
    await panel.getByRole('tab', { name: 'Selection' }).click();
    await panel.locator('#deck-field-1-title').click();
    await page.keyboard.press('End');
    await page.keyboard.type(' again');
    await expect(page.getByTestId('presentation.saveStatus')).toHaveAttribute('data-state', 'invalid', {
      timeout: 15_000,
    });
    await close.click();
    const leave = page.getByRole('dialog');
    await expect(leave).toContainText('Leave without your latest changes?');
    await leave.getByRole('button', { name: 'Keep editing' }).click();
    await expect(panel).toBeVisible();
    await expect(contentTitle).toHaveText('What changes (saved) again');

    // Leave anyway: the unsaved change is dropped, the stored deck is shown
    // (out of edit mode the slides publish no index - the slide on stage is
    // reveal's present section).
    await close.click();
    await page.getByRole('dialog').getByRole('button', { name: 'Leave anyway' }).click();
    await expect(panel).toBeHidden();
    await expect(page.locator('.reveal section.present .deck-slide-header h2')).toHaveText('What changes (saved)');
    expect((await storedDeck(deckId)).spec.slides[1].title).toBe('What changes (saved)');
  });
});
