/**
 * Unit tests for `deck-revision.ts` - `yarn test:unit`.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import { QueryClient } from "@tanstack/react-query";

import { newestDeck, shareNewestDeck } from "./deck-revision.ts";

const deck = (id: string, revision: number, title = "Deck") => ({ id, revision, title });

type Deck = ReturnType<typeof deck>;

test("a write's answer never replaces a newer cached deck", () => {
  // The usual case: the answer is the newest deck, it replaces the cache.
  const saved = deck("d1", 5, "Saved");
  assert.equal(newestDeck(deck("d1", 4), saved), saved);
  assert.equal(newestDeck(undefined, saved), saved);
  // A save committed as 5 but answered after the poll had loaded a
  // collaborator's 6: the cache keeps 6, never rolls back to 5.
  const polled = deck("d1", 6, "Theirs");
  assert.equal(newestDeck(polled, saved), polled);
  // The same revision is replaced: a sharing change does not bump it.
  const shared = deck("d1", 6, "Shared now");
  assert.equal(newestDeck(polled, shared), shared);
  // Another deck under the key is never kept over the answer.
  assert.equal(newestDeck(deck("d2", 9), saved), saved);
});

test("a read answered after a save never takes the cached deck back", async () => {
  // The deck query as `usePresentation` declares it; no GC timer in a test.
  const client = new QueryClient({ defaultOptions: { queries: { gcTime: Infinity, retry: false } } });
  const key = ["presentations", "d1"];
  client.setQueryDefaults(key, { structuralSharing: shareNewestDeck });
  try {
    await client.fetchQuery({ queryKey: key, queryFn: async () => deck("d1", 4, "Before the save") });
    // The poll's read reaches the server - it reads 4 - and is slow to answer...
    let answerRead: (value: Deck) => void = () => undefined;
    const read = client.fetchQuery({
      queryKey: key,
      staleTime: 0,
      queryFn: () => new Promise<Deck>((resolve) => (answerRead = resolve)),
    });
    // ...while the save commits 5 and its answer lands first (`useUpdatePresentation`).
    client.setQueryData<Deck>(key, (cached) => newestDeck(cached, deck("d1", 5, "Saved")));
    const afterSave = client.getQueryData<Deck>(key);
    assert.deepEqual(afterSave, deck("d1", 5, "Saved"));
    // The read lands last, with what it read before the commit: TanStack
    // writes it over the cache when it lands, and the deck's sharing keeps 5 -
    // the very object, so nothing re-renders either.
    answerRead(deck("d1", 4, "Before the save"));
    await read;
    assert.equal(client.getQueryData(key), afterSave);

    // A newer revision read later (a collaborator's 6) is taken.
    await client.fetchQuery({ queryKey: key, staleTime: 0, queryFn: async () => deck("d1", 6, "Theirs") });
    const theirs = client.getQueryData<Deck>(key);
    assert.deepEqual(theirs, deck("d1", 6, "Theirs"));
    // A read of the same revision is shared as ever: nothing new keeps the
    // cached object (no re-render), a changed field - a sharing change does
    // not bump the revision - is taken.
    await client.fetchQuery({ queryKey: key, staleTime: 0, queryFn: async () => deck("d1", 6, "Theirs") });
    assert.equal(client.getQueryData(key), theirs, "an equal read keeps the cached object");
    await client.fetchQuery({ queryKey: key, staleTime: 0, queryFn: async () => deck("d1", 6, "Shared now") });
    assert.equal(client.getQueryData<Deck>(key)?.title, "Shared now");
  } finally {
    client.clear();
  }
});

test("the deck's sharing falls back to TanStack's for anything that is not a deck", () => {
  // No cached deck yet: the answer is taken as is.
  const first = deck("d1", 1);
  assert.equal(shareNewestDeck(undefined, first), first);
  // Equal values keep the cached object; a value without a revision is compared structurally.
  const cached = { items: [1, 2] };
  assert.equal(shareNewestDeck(cached, { items: [1, 2] }), cached);
  assert.deepEqual(shareNewestDeck(cached, { items: [1, 3] }), { items: [1, 3] });
});
