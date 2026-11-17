/**
 * Unit tests for the waiting arcade's achievements - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ACHIEVEMENTS,
  CHEAT_ACHIEVEMENTS,
  achievementById,
  isAchievementId,
  isUntouchedRun,
  statAchievements,
  type RunNumbers,
} from "./achievements.ts";

const EMPTY_RUN: RunNumbers = {
  score: 0,
  wave: 1,
  kills: 0,
  shots: 0,
  hits: 0,
  uiDestroyed: 0,
  bosses: 0,
  maxCombo: 0,
  ufoKills: 0,
  durationMs: 0,
};

test("ids are unique, snake_case, with an English title and description", () => {
  const ids = ACHIEVEMENTS.map((a) => a.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const achievement of ACHIEVEMENTS) {
    assert.match(achievement.id, /^[a-z0-9]+(_[a-z0-9]+)*$/);
    assert.ok(achievement.title.trim().length > 0);
    assert.ok(achievement.description.trim().length > 0);
    // Rendered through ICU (react-intl): a stray brace would break the message.
    assert.ok(!/[{}]/.test(achievement.title + achievement.description), achievement.id);
  }
});

test("lookups agree with the catalog", () => {
  assert.equal(achievementById("lucky_23")?.title, "Lucky 23");
  assert.equal(achievementById("nope"), undefined);
  assert.equal(isAchievementId("first_blood"), true);
  assert.equal(isAchievementId("first-blood"), false);
});

test("cheat runs can only unlock event achievements from the catalog", () => {
  for (const id of CHEAT_ACHIEVEMENTS) {
    assert.equal(achievementById(id)?.kind, "event", id);
  }
});

test("an empty run earns nothing; the thresholds are inclusive", () => {
  assert.deepEqual(statAchievements(EMPTY_RUN), []);
  const run: RunNumbers = {
    score: 10_000,
    wave: 5,
    kills: 100,
    shots: 30,
    hits: 27,
    uiDestroyed: 25,
    bosses: 1,
    maxCombo: 40,
    ufoKills: 5,
    durationMs: 10 * 60 * 1000,
  };
  assert.deepEqual(statAchievements(run).sort(), [
    "boss_slayer",
    "centurion",
    "combo_master",
    "first_blood",
    "marathon",
    "score_10k",
    "sharpshooter",
    "ufo_hunter",
    "wave_5",
    "wrecking_ball",
  ]);
});

test("every stat achievement is reachable and every one it returns is a stat one", () => {
  const huge: RunNumbers = {
    score: 1e6,
    wave: 50,
    kills: 1000,
    shots: 100,
    hits: 100,
    uiDestroyed: 500,
    bosses: 9,
    maxCombo: 100,
    ufoKills: 9,
    durationMs: 3600_000,
  };
  const earned = new Set(statAchievements(huge));
  const statIds = ACHIEVEMENTS.filter((a) => a.kind === "stat").map((a) => a.id);
  assert.deepEqual([...earned].sort(), [...statIds].sort());
});

test("sharpshooter needs both the accuracy and the sample", () => {
  const lucky = { ...EMPTY_RUN, shots: 10, hits: 10 };
  assert.ok(!statAchievements(lucky).includes("sharpshooter"));
  const sloppy = { ...EMPTY_RUN, shots: 100, hits: 89 };
  assert.ok(!statAchievements(sloppy).includes("sharpshooter"));
  const sharp = { ...EMPTY_RUN, shots: 100, hits: 90 };
  assert.ok(statAchievements(sharp).includes("sharpshooter"));
});

test("a game nobody touched has nothing to record, unless it unlocked something new", () => {
  const unlocked = new Set(["hello_there"]);
  assert.equal(isUntouchedRun(false, [], unlocked), true);
  assert.equal(isUntouchedRun(false, ["hello_there"], unlocked), true);
  assert.equal(isUntouchedRun(false, ["hello_there", "deep_thought"], unlocked), false);
  assert.equal(isUntouchedRun(true, [], unlocked), false);
});
