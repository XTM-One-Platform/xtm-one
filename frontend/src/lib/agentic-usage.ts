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
 * The agentic actions the turn in flight has been charged, and how the chat
 * composer's counter reads them.
 *
 * The backend charges one action per metered request BEFORE the agent loop
 * runs and tells the turn's viewers with an `agentic_action` frame
 * (`chat_turn_stream.AGENTIC_ACTION_FRAME`) - a frame type of its own, which
 * clients released before it ignore, where a `status` would land on their
 * working label. A turn that runs several passes (a steering message folded
 * in) is charged once per pass, so the frames add up; a resume replays their
 * totals, one frame per quota period.
 * The counter shows them as the in-progress segment - `4(+1)/10` - until the
 * turn's terminal frame refreshes the quota.
 *
 * No imports on purpose: `yarn test:unit` runs it under plain `node --test`.
 */

export const AGENTIC_ACTION_FRAME = "agentic_action";

/** The actions one `agentic_action` frame charged to the turn in flight. */
interface TurnCharge {
  /** When the frame arrived (client clock, ms). */
  at: number;
  count: number;
  /** The quota period it was counted in: the frame's, else the counter's when it arrived. */
  period?: string;
  /** Relayed: charged to a turn this page did not start (another tab, a REST send, a parent wake). */
  relayed?: true;
}

export type TurnCharges = readonly TurnCharge[];

/** No charge yet: one shared value, so resetting an empty list re-renders nothing. */
export const NO_TURN_CHARGES: TurnCharges = [];

/**
 * The charge one `agentic_action` frame adds, or `null` when it announces
 * none. Only a positive integer count is a charge - the counts the backend
 * journal accepts (`_charged_actions`) - so a malformed frame never shows a
 * phantom action.
 */
export function chargeFromFrame(
  actions: unknown,
  receivedAt: number,
  period?: string,
  relayed = false,
): TurnCharge | null {
  if (typeof actions !== "number" || !Number.isInteger(actions) || actions <= 0) return null;
  const count = actions;
  const charge: TurnCharge = period === undefined ? { at: receivedAt, count } : { at: receivedAt, count, period };
  if (relayed) charge.relayed = true;
  return charge;
}

/**
 * The turn's charges once `charge` is added.
 *
 * A turn is single-flight per conversation, so a charge of a turn this page
 * runs itself proves the relayed charges before it belong to a turn that is
 * over: they are in the period total, not in the reply in progress.
 */
export function addTurnCharge(charges: TurnCharges, charge: TurnCharge): TurnCharges {
  const kept = charge.relayed ? charges : charges.filter((c) => !c.relayed);
  return [...kept, charge];
}

/** The period of the agentic total in a `my-quotas` usage list. */
export function agenticUsagePeriod(
  usage: readonly { usage_type: string; model: string | null; period: string }[] | undefined,
): string | undefined {
  return usage?.find((u) => u.usage_type === "agentic" && u.model === null)?.period;
}

interface AgenticCounter {
  /** Consumed in the period before the turn in flight. */
  before: number;
  /** Charged to the turn in flight. */
  inTurn: number;
  /** Consumed in the period, the turn in flight included. */
  total: number;
}

/**
 * Split the period's count between the turn in flight and what came before it.
 *
 * `count` is the period total of the last `my-quotas` fetch, which succeeded at
 * `fetchedAt` and describes `period`. A charge announced before that fetch is
 * already in its count - the backend sends the frame after charging - and one
 * announced after it is not, until the next fetch. A charge taken in another
 * period (a daily turn running past midnight) sits in that period's total and
 * is left out.
 */
export function splitAgenticCount(
  count: number,
  charges: TurnCharges,
  fetchedAt: number,
  period?: string,
): AgenticCounter {
  let inTurn = 0;
  let counted = 0;
  for (const charge of charges) {
    if (period !== undefined && charge.period !== undefined && charge.period !== period) continue;
    inTurn += charge.count;
    if (charge.at <= fetchedAt) counted += charge.count;
  }
  const before = Math.max(0, count - counted);
  return { before, inTurn, total: before + inTurn };
}

/** The part of a query client `refetchAfterCharge` drives. */
interface QuotaQueryClient {
  cancelQueries(filters: { queryKey: readonly unknown[] }): Promise<void>;
  invalidateQueries(filters: { queryKey: readonly unknown[] }): Promise<void>;
}

/**
 * Re-read the quota after a charge frame, from a request sent after it.
 *
 * `splitAgenticCount` reads a fetch that completed after a charge as holding
 * it, which only holds for a request SENT after the charge. A request still
 * on its way when the frame lands may have read the count before the charge,
 * and an invalidation alone would join it rather than replace it while the
 * query holds no data yet (the first fetch of the page): it is cancelled
 * first, so every response that lands from then on was asked for after the
 * frame.
 */
export async function refetchAfterCharge(client: QuotaQueryClient, queryKey: readonly unknown[]): Promise<void> {
  await client.cancelQueries({ queryKey });
  await client.invalidateQueries({ queryKey });
}
