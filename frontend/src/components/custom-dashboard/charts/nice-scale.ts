/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * The value axis of a chart as ApexCharts lays it out (`Scales.niceScale`, apexcharts 4.7, the
 * library OpenCTI and OpenAEV draw with): its ticks, from the data's least and greatest values,
 * the room the axis has and, when the chart fixes it, how many intervals it shows.
 *
 * Ported for the case those widgets use: no minimum, maximum or step set by the chart. A series of
 * zeros reads 0 to 2; a value of 4 on a horizontal bar asked for one interval reads 0 to 5.
 */

const PRECISION = 1e-11;
/** `niceScaleAllowedMagMsd` (integers and decimals alike in 4.7). */
const ALLOWED_MSD = [1, 1, 2, 5, 5, 5, 10, 10, 10, 10, 10];
/** `niceScaleDefaultTicks`: the intervals by half the ticks the axis has room for. */
const DEFAULT_TICKS = [1, 2, 4, 4, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 12, 12, 12, 12, 12, 12, 12, 12, 12, 24];

function gcd(a: number, b: number, precision = 7): number {
  const big = Math.pow(10, precision - Math.floor(Math.log10(Math.max(Math.abs(a), Math.abs(b)) || 1)));
  let x = Math.round(Math.abs(a) * big);
  let y = Math.round(Math.abs(b) * big);
  while (y) [x, y] = [y, x % y];
  return x / big;
}

function primeFactors(n: number): number[] {
  const factors: number[] = [];
  let value = n;
  for (let divisor = 2; value >= 2;) {
    if (value % divisor === 0) {
      factors.push(divisor);
      value /= divisor;
    } else divisor += 1;
  }
  return factors;
}

function strip(value: number): number {
  return Number(value.toPrecision(7));
}

/** The least and greatest values a value axis shows (rows of `s0`, `s1`...): each series' values, and a stack's totals
 *  (its positive and its negative parts apart); zero when there is none. */
export function valueBounds(
  rows: readonly Record<string, unknown>[],
  count: number,
  inStack: (index: number) => boolean,
): { min: number; max: number } {
  let min = Infinity;
  let max = -Infinity;
  for (const row of rows) {
    let up = 0;
    let down = 0;
    for (let i = 0; i < count; i += 1) {
      const value = row[`s${i}`];
      if (typeof value !== "number" || !Number.isFinite(value)) continue;
      if (inStack(i)) {
        if (value >= 0) up += value;
        else down += value;
      } else {
        min = Math.min(min, value);
        max = Math.max(max, value);
      }
    }
    if (up || down) {
      min = Math.min(min, down, up);
      max = Math.max(max, up, down);
    }
  }
  return Number.isFinite(min) ? { min, max } : { min: 0, max: 0 };
}

export interface NiceScaleInput {
  /** The least and greatest values drawn (a stack's totals for a stacked chart). */
  min: number;
  max: number;
  /** The axis's length in pixels: the chart's width for horizontal bars, its height otherwise. */
  length: number;
  /** Whether the value axis runs along the width (horizontal bars). */
  horizontal: boolean;
  /** The intervals the chart asks for (ApexCharts' `tickAmount`); unset, those its room allows. */
  tickAmount?: number;
}

/** The ticks of the value axis, its first and last being its bounds. */
export function niceScale({ min, max, length, horizontal, tickAmount }: NiceScaleInput): number[] {
  let maxTicks = horizontal ? Math.max((length - 100) / 25, 2) : Math.max((length - 100) / 15, 2);
  if (!Number.isFinite(maxTicks)) maxTicks = 10;
  const gotTickAmount = tickAmount !== undefined;
  let ticks = gotTickAmount ? Math.abs(Math.round(tickAmount)) : DEFAULT_TICKS[Math.min(Math.round(maxTicks / 2), DEFAULT_TICKS.length - 1)];
  let yMin = Number.isFinite(min) ? min : 0;
  let yMax = Number.isFinite(max) ? max : yMin + ticks;
  if (yMin > yMax) [yMin, yMax] = [yMax, yMin];
  else if (yMin === yMax) {
    yMin = yMin === 0 ? 0 : yMin - 1;
    yMax = yMax === 0 ? 2 : yMax + 1;
  }
  if (ticks < 1) ticks = 1;
  let tiks = ticks;
  let range = Math.abs(yMax - yMin);
  // Snap the end close to zero onto it.
  let gotMin = false;
  let gotMax = false;
  if (yMin > 0 && yMin / range < 0.15) {
    yMin = 0;
    gotMin = true;
  }
  if (yMax < 0 && -yMax / range < 0.15) {
    yMax = 0;
    gotMax = true;
  }
  range = Math.abs(yMax - yMin);
  const rough = range / tiks;
  const magPow = Math.pow(10, Math.floor(Math.log10(rough)));
  let stepSize = ALLOWED_MSD[Math.min(Math.ceil(rough / magPow), ALLOWED_MSD.length - 1)] * magPow;
  if (!gotMin && !gotMax) {
    yMin = stepSize * Math.floor(yMin / stepSize);
    yMax = stepSize * Math.ceil(yMax / stepSize);
  } else if (gotMax) {
    if (gotTickAmount) yMin = yMax - stepSize * tiks;
    else {
      const previous = yMin;
      yMin = stepSize * Math.floor(yMin / stepSize);
      if (Math.abs(yMax - yMin) / gcd(range, stepSize) > maxTicks) {
        yMin = yMax - stepSize * ticks;
        yMin += stepSize * Math.floor((previous - yMin) / stepSize);
      }
    }
  } else if (gotTickAmount) yMax = yMin + stepSize * tiks;
  else {
    const previous = yMax;
    yMax = stepSize * Math.ceil(yMax / stepSize);
    if (Math.abs(yMax - yMin) / gcd(range, stepSize) > maxTicks) {
      yMax = yMin + stepSize * ticks;
      yMax += stepSize * Math.ceil((previous - yMax) / stepSize);
    }
  }
  range = Math.abs(yMax - yMin);
  stepSize = gcd(range, stepSize);
  tiks = Math.round(range / stepSize);
  if (!gotTickAmount && !(gotMin || gotMax)) {
    tiks = Math.ceil((range - PRECISION) / (stepSize + PRECISION));
    if (tiks > 16 && primeFactors(tiks).length < 2) tiks += 1;
  }
  if (tiks > maxTicks && !gotTickAmount) {
    // Fewer ticks, each one of the full set's, when the axis is short.
    const factors = primeFactors(tiks);
    const last = factors.length - 1;
    let kept = tiks;
    outer: for (let span = 0; span < last; span += 1) {
      for (let lowest = 0; lowest <= last - span; lowest += 1) {
        let divisor = 1;
        for (let next = lowest; next <= Math.min(lowest + span, last); next += 1) divisor *= factors[next];
        if (tiks / divisor < maxTicks) {
          kept = tiks / divisor;
          break outer;
        }
      }
    }
    stepSize = kept === tiks ? range : range / kept;
  }
  const result: number[] = [];
  const err = stepSize * PRECISION;
  let value = yMin - stepSize;
  do {
    value += stepSize;
    result.push(strip(value));
  } while (yMax - value > err && result.length < 200);
  return result;
}
