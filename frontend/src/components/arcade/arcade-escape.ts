/**
 * What Escape does in the waiting arcade, stage by stage, as the HUD
 * (`Leave the arcade (Esc twice)`) and the hints (`P or Esc to pause - Esc
 * again to leave`) say: the first
 * press pauses, so a stray one never ends a game, and the second leaves - the
 * game ends and its run is saved, exactly as the Leave button does - while `P`
 * resumes. Once the agent's answer is ready, Escape goes to it. Before the game
 * starts and once it is over, Escape leaves at once.
 *
 * No imports on purpose: `yarn test:unit` runs this module under plain
 * `node --test`, which resolves no `@/` alias.
 */
export type ArcadeStage = "intro" | "playing" | "paused" | "over" | "leaving";

/** `quit` ends the game (its run submitted), `answer` is the way back to the answer, `leave` closes. */
type EscapeAction = "pause" | "quit" | "answer" | "leave";

export function escapeAction(stage: ArcadeStage, answerReady: boolean): EscapeAction | null {
  switch (stage) {
    case "playing":
      return answerReady ? "answer" : "pause";
    case "paused":
      return answerReady ? "answer" : "quit";
    case "intro":
    case "over":
      return "leave";
    case "leaving":
      return null;
  }
}
