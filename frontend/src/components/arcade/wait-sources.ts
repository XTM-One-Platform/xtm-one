/**
 * Which chat surfaces are waiting on their agent, and whether the
 * answer a game waits for has arrived.
 *
 * Several surfaces can wait at once (the main chat, the presentation studio
 * rail). Each reports the conversation on its screen and whether its agent
 * works on a turn there. A game waits for the turn running on the surface it
 * was launched from or - launched from the palette or the code - for every
 * turn running at that moment, and its answer is ready once one of them
 * ENDS where it runs: the turn's final `done`, an error or a cancel. Nothing
 * else is an answer:
 *
 * - an intermediate segment does not end its turn: the chat keeps
 *   working through the tool calls that follow;
 * - a surface that leaves the conversation (Back / Forward, a switch, a new
 *   conversation) or goes away (unmounted) no longer waits for that turn,
 *   even if it comes back to it: the game plays on, with no answer to wait
 *   for;
 * - a turn that starts after the game started, wherever it runs, is not the
 *   one it waits for, and a game started while nothing waited never hears of
 *   an answer.
 *
 * Once ready, the answer stays ready until the game ends.
 */
export type ArcadeAnswer = "none" | "waiting" | "ready";

/** What a chat surface reports (`useArcadeWaitSignal`). */
export interface SurfaceWait {
  /** The conversation on the surface's screen (`null`: a new one, not created yet). */
  conversationId: string | null;
  /**
   * Its agent works on a turn there: the surface's working state
   * (`useTurnFrames().working`, handed to `XtmChatPanel` as `working`): the
   * panel's own running turn (`onTurnRunningChange`), held through
   * intermediate segments, dropped by the turn's final `done`, an error or a
   * cancel. Never the streamed text: a segment streams exactly like a final
   * answer.
   */
  working: boolean;
}

interface SurfaceState {
  conversationId: string | null;
  /** The turn running on the surface (unique to this registry), `null` while idle. */
  turn: number | null;
}

export class WaitSources {
  private readonly surfaces = new Map<string, SurfaceState>();
  /** Surface id -> the turn the running game waits for there. */
  private watched: Map<string, number> | null = null;
  private ready = false;
  private nextTurn = 1;

  report(id: string, { conversationId, working }: SurfaceWait): void {
    const previous = this.surfaces.get(id);
    // A new conversation getting its id while its first turn runs is the one
    // that turn created, not a conversation the surface moved to.
    const stayed =
      previous !== undefined &&
      (previous.conversationId === conversationId || (previous.conversationId === null && previous.turn !== null));
    const turn = !working ? null : stayed && previous.turn !== null ? previous.turn : this.nextTurn++;
    this.surfaces.set(id, { conversationId, turn });
    const awaited = this.watched?.get(id);
    if (awaited === undefined || awaited === turn) return;
    // The awaited turn is over for this surface: it ended where it ran - the
    // answer - or the surface left its conversation.
    this.watched?.delete(id);
    if (stayed && turn === null) this.ready = true;
  }

  /** The surface is gone: it will not answer. */
  drop(id: string): void {
    this.surfaces.delete(id);
    this.watched?.delete(id);
  }

  /** At least one surface is waiting. */
  get any(): boolean {
    for (const surface of this.surfaces.values()) if (surface.turn !== null) return true;
    return false;
  }

  /** A game starts: remember the turns it waits for. */
  watch(launchedFrom?: string | null): void {
    const running: [string, number][] = [];
    for (const [id, surface] of this.surfaces) if (surface.turn !== null) running.push([id, surface.turn]);
    const own = running.filter(([id]) => id === launchedFrom);
    this.watched = new Map(own.length > 0 ? own : running);
    this.ready = false;
  }

  unwatch(): void {
    this.watched = null;
    this.ready = false;
  }

  get answer(): ArcadeAnswer {
    if (this.ready) return "ready";
    return this.watched && this.watched.size > 0 ? "waiting" : "none";
  }
}
