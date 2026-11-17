/**
 * The Video Studio player's rules - `yarn test:unit`.
 *
 * The clock reads like a player's, the keys are a video player's (and leave
 * the browser's shortcuts alone), the speeds step and hold at the ends, and
 * the remembered volume survives a broken or missing store.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  bufferedEnd,
  captionLift,
  cueAtTop,
  cueText,
  formatPlayerTime,
  letterbox,
  nextSpeed,
  playerKeyAction,
  readPlayerVolume,
  shareAt,
  tapSide,
  writePlayerVolume,
} from "./player-model.ts";

test("the clock reads minutes and seconds, and hours only past one", () => {
  assert.equal(formatPlayerTime(0), "0:00");
  assert.equal(formatPlayerTime(5.9), "0:05");
  assert.equal(formatPlayerTime(130), "2:10");
  assert.equal(formatPlayerTime(3723), "1:02:03");
  assert.equal(formatPlayerTime(Number.NaN), "0:00");
  assert.equal(formatPlayerTime(-4), "0:00");
});

test("the keys are a video player's", () => {
  assert.deepEqual(playerKeyAction({ key: " " }), { type: "toggle" });
  assert.deepEqual(playerKeyAction({ key: "K" }), { type: "toggle" });
  assert.deepEqual(playerKeyAction({ key: "ArrowLeft" }), { type: "seekBy", seconds: -5 });
  assert.deepEqual(playerKeyAction({ key: "l" }), { type: "seekBy", seconds: 10 });
  assert.deepEqual(playerKeyAction({ key: "ArrowUp" }), { type: "volumeBy", delta: 0.1 });
  assert.deepEqual(playerKeyAction({ key: "7" }), { type: "seekTo", share: 0.7 });
  assert.deepEqual(playerKeyAction({ key: "End" }), { type: "seekTo", share: 1 });
  assert.deepEqual(playerKeyAction({ key: ">" }), { type: "speed", direction: 1 });
  assert.deepEqual(playerKeyAction({ key: "f" }), { type: "fullscreen" });
  assert.deepEqual(playerKeyAction({ key: "," }), { type: "frame", direction: -1 });
  assert.deepEqual(playerKeyAction({ key: "." }), { type: "frame", direction: 1 });
  assert.deepEqual(playerKeyAction({ key: "?" }), { type: "shortcuts" });
  assert.equal(playerKeyAction({ key: "x" }), null);
});

test("a tap on a touch screen skips back on its left third, forward on its right one", () => {
  assert.equal(tapSide(0.1), -1);
  assert.equal(tapSide(0.5), 0);
  assert.equal(tapSide(0.9), 1);
});

test("a modified key stays the browser's", () => {
  assert.equal(playerKeyAction({ key: "f", ctrlKey: true }), null);
  assert.equal(playerKeyAction({ key: "ArrowLeft", altKey: true }), null);
  assert.equal(playerKeyAction({ key: "k", metaKey: true }), null);
});

test("the speed steps through the offered ones and holds at the ends", () => {
  assert.equal(nextSpeed(1, 1), 1.25);
  assert.equal(nextSpeed(1, -1), 0.75);
  assert.equal(nextSpeed(2, 1), 2);
  assert.equal(nextSpeed(0.5, -1), 0.5);
  // A rate set elsewhere steps to its neighbour.
  assert.equal(nextSpeed(1.1, 1), 1.25);
  assert.equal(nextSpeed(1.1, -1), 1);
});

test("a pointer on a track is a share of it, held inside", () => {
  assert.equal(shareAt(150, 100, 200), 0.25);
  assert.equal(shareAt(50, 100, 200), 0);
  assert.equal(shareAt(400, 100, 200), 1);
  assert.equal(shareAt(150, 100, 0), 0);
});

test("the picture is letterboxed in the player, 16:9 until its shape is known", () => {
  // A 16:9 render in a portrait window: bands above and below.
  assert.deepEqual(letterbox({ width: 720, height: 1000 }, { width: 1920, height: 1080 }), { left: 0, top: 297.5, width: 720, height: 405 });
  // A vertical video on a wide screen: bands on the sides.
  assert.deepEqual(letterbox({ width: 1600, height: 900 }, { width: 1080, height: 1920 }), { left: 546.875, top: 0, width: 506.25, height: 900 });
  assert.deepEqual(letterbox({ width: 1600, height: 900 }, { width: 0, height: 0 }), { left: 0, top: 0, width: 1600, height: 900 });
  assert.deepEqual(letterbox({ width: 0, height: 900 }, { width: 16, height: 9 }), { left: 0, top: 0, width: 0, height: 0 });
});

test("a caption moves only as far as the controls reach past it", () => {
  assert.equal(captionLift(20, 76), 56);
  assert.equal(captionLift(300, 76), 0);
  assert.equal(captionLift(75.2, 76), 1);
});

test("a cue says where it sits and what it reads", () => {
  // The render writes `line:0` for captions at the top, nothing for the bottom.
  assert.equal(cueAtTop({ line: 0, snapToLines: true }), true);
  assert.equal(cueAtTop({ line: "auto", snapToLines: true }), false);
  assert.equal(cueAtTop({ line: -2, snapToLines: true }), false);
  assert.equal(cueAtTop({ line: 10, snapToLines: false }), true);
  assert.equal(cueAtTop({ line: 90, snapToLines: false }), false);
  assert.equal(cueText("<v Ada>Hello <b>there</b></v> &amp; welcome &lt;3"), "Hello there & welcome <3");
  assert.equal(cueText("Two\nlines"), "Two\nlines");
});

test("the buffered end is the one of the range the playhead is in", () => {
  const ranges = (...pairs: [number, number][]) => ({
    length: pairs.length,
    start: (i: number) => pairs[i][0],
    end: (i: number) => pairs[i][1],
  });
  assert.equal(bufferedEnd(ranges([0, 12], [40, 60]), 5), 12);
  assert.equal(bufferedEnd(ranges([0, 12], [40, 60]), 45), 60);
  assert.equal(bufferedEnd(ranges([0, 12], [40, 60]), 20), 0);
  assert.equal(bufferedEnd(ranges(), 3), 0);
});

test("the volume is remembered, and a broken or missing store means full volume", () => {
  const store = new Map<string, string>();
  const storage = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
  assert.deepEqual(readPlayerVolume(storage), { volume: 1, muted: false });
  writePlayerVolume(storage, { volume: 0.35, muted: true });
  assert.deepEqual(readPlayerVolume(storage), { volume: 0.35, muted: true });
  store.set("xtm-video-player-volume", "{not json");
  assert.deepEqual(readPlayerVolume(storage), { volume: 1, muted: false });
  store.set("xtm-video-player-volume", JSON.stringify({ volume: 7 }));
  assert.deepEqual(readPlayerVolume(storage), { volume: 1, muted: false });
  assert.deepEqual(readPlayerVolume(undefined), { volume: 1, muted: false });
});
