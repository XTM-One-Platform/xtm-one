/**
 * Unit tests for the waiting arcade's scoring rules - `yarn test:unit`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BOSS_POINTS_BASE,
  BOSS_POINTS_MAX,
  BOSS_POINTS_STEP,
  BOSS_ROTATION,
  BOSS_WAVE_EVERY,
  EXTRA_LIFE_EVERY,
  EXTRA_LIFE_FIRST,
  FASTEST_WAVE_SECONDS,
  FIRE_COOLDOWN_MS,
  INVADER_POINTS,
  MAX_MULTIPLIER,
  MAX_POINTS_PER_SECOND,
  MAX_SHOTS_PER_SECOND,
  NUKE_UI_POINTS,
  POWER_UP_WEIGHTS,
  RAPID_FIRE_COOLDOWN_MS,
  SCORE_ALLOWANCE,
  SHOT_ALLOWANCE,
  SPREAD_ANGLES,
  SURVIVAL_BONUS_MAX,
  THROTTLED_FIRE_COOLDOWN_MS,
  UFO_DUCK_POINTS,
  UFO_LEGENDARY_POINTS,
  UFO_PI_POINTS,
  UI_DESTROYED_POINTS,
  WAVE_CLEAR_BONUS_BASE,
  WAVE_CLEAR_BONUS_MAX,
  WAVE_CLEAR_BONUS_STEP,
  bossMaxHp,
  bossPoints,
  dropChance,
  extraLivesEarned,
  isLegendaryShot,
  maxPlausibleScore,
  maxWavePoints,
  multiplierFor,
  pickPowerUp,
  stepIntervalMs,
  survivalBonus,
  ufoPoints,
  waveClearBonus,
  waveConfig,
  type PowerUpKind,
} from "./rules.ts";

test("the saucer pays 300 on the 23rd shot and every 15th after, never before", () => {
  const legendary = [];
  for (let shot = 1; shot <= 100; shot += 1) {
    if (ufoPoints(shot) === 300) legendary.push(shot);
    assert.equal(isLegendaryShot(shot), ufoPoints(shot) === 300);
  }
  assert.deepEqual(legendary, [23, 38, 53, 68, 83, 98]);
  for (let shot = 1; shot <= 200; shot += 1) {
    assert.ok([50, 100, 150, 300].includes(ufoPoints(shot)), `shot ${shot}`);
  }
});

test("the multiplier climbs every five hits and caps", () => {
  assert.equal(multiplierFor(0), 1);
  assert.equal(multiplierFor(4), 1);
  assert.equal(multiplierFor(5), 2);
  assert.equal(multiplierFor(14), 3);
  assert.equal(multiplierFor(1000), MAX_MULTIPLIER);
  assert.equal(multiplierFor(-3), 1);
});

test("bonus lives: the classic 1,500 base, then one per 20,000", () => {
  assert.equal(extraLivesEarned(0), 0);
  assert.equal(extraLivesEarned(EXTRA_LIFE_FIRST - 1), 0);
  assert.equal(extraLivesEarned(EXTRA_LIFE_FIRST), 1);
  assert.equal(extraLivesEarned(EXTRA_LIFE_FIRST + EXTRA_LIFE_EVERY - 1), 1);
  assert.equal(extraLivesEarned(EXTRA_LIFE_FIRST + EXTRA_LIFE_EVERY), 2);
});

test("every fifth wave is a boss, in rotation, tougher each time round", () => {
  const bosses = [5, 10, 15, 20, 25].map((w) => waveConfig(w).boss);
  assert.deepEqual(bosses, [...BOSS_ROTATION, BOSS_ROTATION[0]]);
  assert.equal(waveConfig(4).boss, null);
  assert.equal(waveConfig(25).bossCycle, 1);
  assert.ok(bossMaxHp("hallucination", 1) > bossMaxHp("hallucination", 0));
  assert.equal(bossPoints(5), 2500);
  assert.ok(bossPoints(100) <= 5000);
});

test("waves get harder, within bounds", () => {
  const first = waveConfig(1);
  const late = waveConfig(30);
  assert.equal(first.startDrop, 0);
  assert.ok(late.startDrop <= 4);
  assert.ok(late.stepFactor < first.stepFactor && late.stepFactor >= 0.45);
  assert.ok(late.fireIntervalMs < first.fireIntervalMs && late.fireIntervalMs >= 350);
  assert.ok(late.maxEnemyBullets <= 6);
  assert.ok(late.specialChance <= 0.12);
  assert.equal(waveConfig(5).armoredTopRow, false);
  assert.equal(waveConfig(6).armoredTopRow, true);
  assert.ok(waveClearBonus(100) <= 2000);
});

test("the march speeds up as the formation thins out", () => {
  assert.equal(stepIntervalMs(55, 1), 880);
  assert.ok(stepIntervalMs(10, 1) < stepIntervalMs(55, 1));
  assert.equal(stepIntervalMs(1, 1), 18);
  assert.equal(stepIntervalMs(1000, 1), 900);
});

test("power-up picks follow the weights and stay in range", () => {
  const counts = new Map<PowerUpKind, number>();
  const samples = 20000;
  for (let i = 0; i < samples; i += 1) {
    const kind = pickPowerUp(i / samples);
    counts.set(kind, (counts.get(kind) ?? 0) + 1);
  }
  const total = Object.values(POWER_UP_WEIGHTS).reduce((a, b) => a + b, 0);
  for (const [kind, weight] of Object.entries(POWER_UP_WEIGHTS) as [PowerUpKind, number][]) {
    const share = (counts.get(kind) ?? 0) / samples;
    assert.ok(Math.abs(share - weight / total) < 0.01, `${kind}: ${share}`);
  }
  assert.equal(pickPowerUp(-1), pickPowerUp(0));
  assert.equal(pickPowerUp(2), pickPowerUp(0.999999));
  assert.equal(dropChance("bug", 1), 1);
  assert.ok(dropChance("octopus", 99) <= 0.07);
});

test("the waiting bonus grows with the wait and caps", () => {
  assert.equal(survivalBonus(0), 500);
  assert.equal(survivalBonus(60), 1100);
  assert.equal(survivalBonus(100000), 3000);
});

test("no wave can out-score the server's plausibility bound", () => {
  // The server refuses a run above SCORE_ALLOWANCE + MAX_POINTS_PER_SECOND * seconds.
  // Even the densest wave, cleared at the cannon's best rate, must stay under it.
  for (let wave = 1; wave <= 60; wave += 1) {
    const rate = maxWavePoints(wave) / FASTEST_WAVE_SECONDS;
    assert.ok(rate < MAX_POINTS_PER_SECOND, `wave ${wave}: ${rate} points per second`);
  }
  assert.equal(maxPlausibleScore(0), SCORE_ALLOWANCE);
  assert.equal(maxPlausibleScore(10), SCORE_ALLOWANCE + 10 * MAX_POINTS_PER_SECOND);
  assert.equal(maxPlausibleScore(-5), SCORE_ALLOWANCE);
});

test("the scoring functions are the formulas the server's per-wave ceiling rebuilds", () => {
  // backend/tests/test_arcade.py reads the named values; these tie the game's
  // functions to them, so a value cannot be raised around its constant.
  for (let wave = 1; wave <= 300; wave += 1) {
    assert.equal(
      waveClearBonus(wave),
      Math.min(WAVE_CLEAR_BONUS_MAX, WAVE_CLEAR_BONUS_BASE + WAVE_CLEAR_BONUS_STEP * (wave - 1)),
      `clear bonus of wave ${wave}`,
    );
    const boss = waveConfig(wave).boss;
    assert.equal(boss !== null, wave % BOSS_WAVE_EVERY === 0, `wave ${wave}`);
    if (boss) {
      const nth = wave / BOSS_WAVE_EVERY;
      assert.equal(bossPoints(wave), Math.min(BOSS_POINTS_MAX, BOSS_POINTS_BASE + BOSS_POINTS_STEP * (nth - 1)));
    }
  }
  for (const seconds of [0, 1, 60, 250, 86_400, Number.MAX_SAFE_INTEGER]) {
    assert.ok(survivalBonus(seconds) <= SURVIVAL_BONUS_MAX, `waiting ${seconds} s`);
  }
  const bestSaucer = Math.max(UFO_LEGENDARY_POINTS, UFO_DUCK_POINTS, UFO_PI_POINTS);
  for (let shot = 1; shot <= 1000; shot += 1) assert.ok(ufoPoints(shot) <= bestSaucer, `shot ${shot}`);
  assert.equal(multiplierFor(Number.MAX_SAFE_INTEGER), MAX_MULTIPLIER);
  assert.ok(Object.values(INVADER_POINTS).every((points) => points <= INVADER_POINTS.golden));
  assert.ok(NUKE_UI_POINTS <= UI_DESTROYED_POINTS);
});

test("the cannon never out-fires the server's shot bound", () => {
  // The server refuses more than SHOT_ALLOWANCE + MAX_SHOTS_PER_SECOND * seconds.
  const fastest = (SPREAD_ANGLES.length * 1000) / RAPID_FIRE_COOLDOWN_MS;
  assert.ok(fastest < MAX_SHOTS_PER_SECOND, `${fastest} shots per second`);
  assert.ok(RAPID_FIRE_COOLDOWN_MS < FIRE_COOLDOWN_MS && FIRE_COOLDOWN_MS < THROTTLED_FIRE_COOLDOWN_MS);
  assert.ok(SHOT_ALLOWANCE > 0);
});
