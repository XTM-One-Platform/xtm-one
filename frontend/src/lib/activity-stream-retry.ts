// SPDX-License-Identifier: Apache-2.0

/**
 * When the activity stream reconnects, and whether it asks first.
 *
 * An `EventSource` never exposes the HTTP status of a failed connection, so a
 * refused session and a restarting server look the same to it. Reconnecting
 * on every error every 5 seconds made a tab whose token had died retry that
 * token forever: a hidden tab makes no other request, so the API client's 401
 * handling - the one path that ends a dead session - never ran.
 *
 * A stream that was open and ended (its bounded lifetime on the server, a
 * deploy, a network drop) reconnects after the usual delay. A connection that
 * failed before it opened is followed by a session check through the API
 * client, then by an exponentially growing wait.
 *
 * Every wait is spread between half and one and a half times its nominal
 * value: the streams a restarting worker drops all end at the same moment, and
 * a fixed delay brought every one of those tabs back within the same second.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */

/** Delay before reopening a stream that was open and ended. */
export const STREAM_RECONNECT_DELAY_MS = 5_000;

/** Ceiling of the wait between connections that fail before they open. */
export const STREAM_MAX_RETRY_DELAY_MS = 5 * 60_000;

type StreamRetry = {
  /** Wait this long before connecting again. */
  delayMs: number;
  /** Ask the server whether the session still holds before connecting again. */
  checkSession: boolean;
};

/**
 * The retry after a stream error. `failures` is the number of connections in
 * a row that failed before they opened, the one that just failed included;
 * `0` when the stream that ended had opened. `random` returns a number in
 * `[0, 1)`; `0.5` gives the nominal delay.
 */
export function streamRetry(failures: number, random: () => number = Math.random): StreamRetry {
  const nominal =
    failures <= 0
      ? STREAM_RECONNECT_DELAY_MS
      : Math.min(
          STREAM_RECONNECT_DELAY_MS * 2 ** Math.min(failures - 1, 16),
          STREAM_MAX_RETRY_DELAY_MS,
        );
  return {
    delayMs: Math.round(nominal * (0.5 + random())),
    checkSession: failures > 0,
  };
}
