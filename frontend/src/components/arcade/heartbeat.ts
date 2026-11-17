/**
 * The live room's heartbeat: how often a playing client tells the
 * server it is still at it.
 *
 * The server decides (`HEARTBEAT_SECONDS` in `backend/app/services/arcade.py`)
 * and says so with the session a game starts on and with every beat's answer;
 * the client only keeps the value in a sane range and falls back to the
 * server's own default while it has heard nothing
 * (`backend/tests/test_arcade.py` checks the two agree).
 */

/** The server's cadence, used until it says otherwise. */
export const DEFAULT_HEARTBEAT_SECONDS = 10;
/** Whatever a server says, a client never beats faster or slower than this. */
export const MIN_HEARTBEAT_SECONDS = 5;
export const MAX_HEARTBEAT_SECONDS = 60;
/** The first beat, soon after the game starts. */
export const FIRST_HEARTBEAT_MS = 1500;

/** Milliseconds between two beats for the cadence the server sent, if any. */
export function heartbeatIntervalMs(serverSeconds: number | null | undefined): number {
  const seconds =
    typeof serverSeconds === "number" && Number.isFinite(serverSeconds) && serverSeconds > 0
      ? Math.min(MAX_HEARTBEAT_SECONDS, Math.max(MIN_HEARTBEAT_SECONDS, serverSeconds))
      : DEFAULT_HEARTBEAT_SECONDS;
  return Math.round(seconds * 1000);
}

/**
 * How long a refused beat asks the loop to hold (`{ retryAfterSeconds }`, from
 * the server's `Retry-After`), in milliseconds: 0 when it asks nothing, and
 * never more than the slowest cadence, so no answer can stall the loop.
 */
export function retryAfterMs(answer: unknown): number {
  const seconds = (answer as { retryAfterSeconds?: unknown } | null | undefined)?.retryAfterSeconds;
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return 0;
  return Math.round(Math.min(seconds, MAX_HEARTBEAT_SECONDS) * 1000);
}

export interface HeartbeatTimers {
  set: (callback: () => void, ms: number) => number;
  clear: (id: number) => void;
  now: () => number;
}

/**
 * Beat once after `FIRST_HEARTBEAT_MS`, then every `intervalMs()`.
 *
 * The next wait is set the moment a beat is sent, so a request that hangs never
 * stalls the loop. When the beat's answer (the promise `beat` returns) changes
 * the cadence, that wait is set again at the new length, counted from the beat:
 * the server paces the very next beat. An answer that arrives after the next
 * beat already left changes nothing, and a beat that throws is only missed.
 * An answer carrying a `Retry-After` (a beat the server refused as too fast)
 * holds the next beat at least that long, counted from the answer.
 * Returns the function that stops the loop.
 */
export function startHeartbeat(
  beat: () => PromiseLike<unknown> | undefined,
  intervalMs: () => number,
  timers: HeartbeatTimers = {
    set: (callback, ms) => window.setTimeout(callback, ms),
    clear: (id) => window.clearTimeout(id),
    now: () => performance.now(),
  },
): () => void {
  let stopped = false;
  let id = 0;
  let sent = 0;
  const wait = (ms: number) => {
    id = timers.set(tick, Math.max(0, ms));
  };
  function tick() {
    if (stopped) return;
    const sentAt = timers.now();
    const beatNumber = ++sent;
    let answered: PromiseLike<unknown> | undefined;
    try {
      answered = beat();
    } catch {
      // A missed beat, never the end of the loop: the next wait is still set below.
      answered = undefined;
    }
    if (stopped) return;
    const planned = intervalMs();
    wait(planned);
    answered?.then(
      (answer) => {
        if (stopped || beatNumber !== sent) return;
        const hold = retryAfterMs(answer);
        const wanted = intervalMs();
        if (wanted === planned && hold === 0) return;
        timers.clear(id);
        wait(Math.max(wanted - (timers.now() - sentAt), hold));
      },
      () => undefined,
    );
  }
  wait(FIRST_HEARTBEAT_MS);
  return () => {
    stopped = true;
    timers.clear(id);
  };
}
