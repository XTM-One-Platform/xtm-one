/**
 * The waiting arcade's achievements - pure, unit-tested.
 *
 * Two kinds, and the server treats them differently:
 *  - `stat`: proven by the run's own numbers. `statAchievements()` mirrors the
 *    server's `STAT_ACHIEVEMENTS`, which re-checks every one of them.
 *  - `event`: tied to something only the game sees (a secret code, the
 *    saucer's legendary shot, destroying the logo...). The server accepts them
 *    from a closed list - they are badges, not points.
 *
 * `backend/tests/test_arcade.py` reads the ids and kinds from this file and
 * compares them with `backend/app/services/arcade.py`, so adding one here
 * without the server (or the reverse) fails the build.
 *
 * `title` and `description` are English i18n ids, translated where displayed.
 */

type AchievementKind = "stat" | "event";
export type AchievementCategory = "skill" | "secret" | "destruction" | "waiting" | "boss" | "calendar";

interface AchievementDef {
  id: string;
  kind: AchievementKind;
  category: AchievementCategory;
  title: string;
  description: string;
  /** Shown as "???" until unlocked. */
  secret: boolean;
}

export const ACHIEVEMENTS = [
  // ── Skill (stat) ──
  { id: "first_blood", kind: "stat", category: "skill", title: "First Blood", description: "Destroy your first invader.", secret: false },
  { id: "centurion", kind: "stat", category: "skill", title: "Centurion", description: "Destroy 100 invaders in a single game.", secret: false },
  { id: "wave_5", kind: "stat", category: "skill", title: "Wave Rider", description: "Reach wave 5.", secret: false },
  { id: "wave_10", kind: "stat", category: "skill", title: "Deep Space", description: "Reach wave 10.", secret: false },
  { id: "score_10k", kind: "stat", category: "skill", title: "Five Digits", description: "Score 10,000 points in one game.", secret: false },
  { id: "score_50k", kind: "stat", category: "skill", title: "High Roller", description: "Score 50,000 points in one game.", secret: false },
  { id: "score_100k", kind: "stat", category: "skill", title: "Living Legend", description: "Score 100,000 points in one game.", secret: false },
  { id: "sharpshooter", kind: "stat", category: "skill", title: "Sharpshooter", description: "Finish a game with 90% accuracy over at least 30 shots.", secret: false },
  { id: "combo_master", kind: "stat", category: "skill", title: "Combo Master", description: "Chain 40 hits without a miss.", secret: false },
  { id: "boss_slayer", kind: "stat", category: "boss", title: "Boss Slayer", description: "Defeat a boss.", secret: false },
  { id: "ufo_hunter", kind: "stat", category: "skill", title: "Saucer Hunter", description: "Shoot down 5 mystery saucers in one game.", secret: false },
  { id: "wrecking_ball", kind: "stat", category: "destruction", title: "Wrecking Ball", description: "Destroy 25 interface elements in one game.", secret: false },
  { id: "total_annihilation", kind: "stat", category: "destruction", title: "Total Annihilation", description: "Destroy 100 interface elements in one game.", secret: false },
  { id: "marathon", kind: "stat", category: "skill", title: "Marathon", description: "Play a single game for 10 minutes.", secret: false },

  // ── Secret codes (event) ──
  { id: "konami_kid", kind: "event", category: "secret", title: "Konami Kid", description: "Enter the most famous cheat code in gaming history.", secret: true },
  { id: "rainbow_road", kind: "event", category: "secret", title: "Rainbow Road", description: "Enter it again, in the middle of a game.", secret: true },
  { id: "god_complex", kind: "event", category: "secret", title: "God Complex", description: "Type the code that made a 1993 shooter famous.", secret: true },
  { id: "arsenal", kind: "event", category: "secret", title: "Full Arsenal", description: "Every weapon, all at once.", secret: true },
  { id: "deep_thought", kind: "event", category: "secret", title: "Deep Thought", description: "Type the answer to life, the universe and everything.", secret: true },
  { id: "hello_there", kind: "event", category: "secret", title: "Hello There", description: "Say hello to the invaders.", secret: true },
  { id: "adventurer", kind: "event", category: "secret", title: "Adventurer", description: "A hollow voice says: nothing happens.", secret: true },
  { id: "permission_denied", kind: "event", category: "secret", title: "Permission Denied", description: "Try to sudo your way out.", secret: true },

  // ── Saucers (event) ──
  { id: "lucky_23", kind: "event", category: "secret", title: "Lucky 23", description: "Hit the mystery saucer for 300 points.", secret: true },
  { id: "rubber_duck", kind: "event", category: "secret", title: "Rubber Duck Debugging", description: "Shoot down the rarest saucer of all.", secret: true },
  { id: "pi_hunter", kind: "event", category: "calendar", title: "Pi Hunter", description: "Hit the saucer for 314 points, on the right day.", secret: true },

  // ── Destroying the interface (event) ──
  { id: "self_sabotage", kind: "event", category: "destruction", title: "Self-Sabotage", description: "Destroy one of your own messages.", secret: true },
  { id: "shoot_the_messenger", kind: "event", category: "destruction", title: "Shoot the Messenger", description: "Destroy one of the assistant's messages.", secret: true },
  { id: "brand_vandal", kind: "event", category: "destruction", title: "Brand Vandal", description: "Destroy the logo.", secret: true },
  { id: "radio_silence", kind: "event", category: "destruction", title: "Radio Silence", description: "Destroy the message composer.", secret: true },
  { id: "navigator_down", kind: "event", category: "destruction", title: "Navigator Down", description: "Destroy a piece of the navigation.", secret: true },
  { id: "nuked", kind: "event", category: "destruction", title: "Tactical Nuke", description: "Wipe the whole screen at once.", secret: true },
  { id: "sudo", kind: "event", category: "destruction", title: "Superuser", description: "Play a round in sudo mode.", secret: true },

  // ── Play style (event) ──
  { id: "pacifist", kind: "event", category: "skill", title: "Pacifist", description: "Survive 30 seconds without firing a shot.", secret: false },
  { id: "last_stand", kind: "event", category: "skill", title: "Last Stand", description: "Clear a wave with your very last life.", secret: false },
  { id: "speed_demon", kind: "event", category: "skill", title: "Speed Demon", description: "Clear the first wave in under 25 seconds.", secret: false },
  { id: "untouchable", kind: "event", category: "skill", title: "Untouchable", description: "Clear 3 waves in a row without losing a life.", secret: false },

  // ── Bosses (event) ──
  { id: "hallucination_buster", kind: "event", category: "boss", title: "Reality Check", description: "Defeat the Hallucination.", secret: false },
  { id: "rate_limit_exceeded", kind: "event", category: "boss", title: "429 Defeated", description: "Defeat the Rate Limiter.", secret: false },
  { id: "injection_proof", kind: "event", category: "boss", title: "Injection Proof", description: "Defeat the Prompt Injector.", secret: false },
  { id: "overflow_handled", kind: "event", category: "boss", title: "Overflow Handled", description: "Defeat the Token Overflow.", secret: false },

  // ── Waiting for the agent (event) ──
  { id: "patience", kind: "event", category: "waiting", title: "Patience of a Saint", description: "Play for 3 minutes while your agent works.", secret: false },
  { id: "overtime", kind: "event", category: "waiting", title: "Overtime", description: "Keep playing after your answer arrived.", secret: false },
  { id: "mission_complete", kind: "event", category: "waiting", title: "Mission Complete", description: "Survive until the agent's answer arrives.", secret: false },

  // ── Calendar (event) ──
  { id: "night_owl", kind: "event", category: "calendar", title: "Night Owl", description: "Play between midnight and 5 a.m.", secret: true },
  { id: "early_bird", kind: "event", category: "calendar", title: "Early Bird", description: "Play between 5 and 7 a.m.", secret: true },
  { id: "friday_deploy", kind: "event", category: "calendar", title: "Friday Deploy", description: "Play on a Friday evening instead of deploying.", secret: true },
  { id: "trick_or_treat", kind: "event", category: "calendar", title: "Trick or Treat", description: "Play during Halloween week.", secret: true },
  { id: "ho_ho_ho", kind: "event", category: "calendar", title: "Ho Ho Ho", description: "Play during the holiday season.", secret: true },
  { id: "april_fool", kind: "event", category: "calendar", title: "April Fool", description: "Play on April 1st.", secret: true },
] as const satisfies readonly AchievementDef[];

export type AchievementId = (typeof ACHIEVEMENTS)[number]["id"];

const BY_ID: ReadonlyMap<string, AchievementDef> = new Map(ACHIEVEMENTS.map((a) => [a.id, a]));

export function achievementById(id: string): AchievementDef | undefined {
  return BY_ID.get(id);
}

export function isAchievementId(id: string): id is AchievementId {
  return BY_ID.has(id);
}

/** The only achievements a run played with a cheat code can unlock. */
export const CHEAT_ACHIEVEMENTS: ReadonlySet<AchievementId> = new Set<AchievementId>([
  "konami_kid",
  "rainbow_road",
  "god_complex",
  "arsenal",
  "deep_thought",
  "hello_there",
  "adventurer",
  "permission_denied",
]);

/** The numbers of a run, as the game tallies them. */
export interface RunNumbers {
  score: number;
  wave: number;
  kills: number;
  shots: number;
  hits: number;
  uiDestroyed: number;
  bosses: number;
  maxCombo: number;
  ufoKills: number;
  durationMs: number;
}

/** The stat achievements these numbers prove - the server's own check, mirrored. */
export function statAchievements(run: RunNumbers): AchievementId[] {
  const accuracy = run.shots > 0 ? run.hits / run.shots : 0;
  const earned: AchievementId[] = [];
  const check = (id: AchievementId, ok: boolean) => {
    if (ok) earned.push(id);
  };
  check("first_blood", run.kills >= 1);
  check("centurion", run.kills >= 100);
  check("wave_5", run.wave >= 5);
  check("wave_10", run.wave >= 10);
  check("score_10k", run.score >= 10_000);
  check("score_50k", run.score >= 50_000);
  check("score_100k", run.score >= 100_000);
  check("sharpshooter", run.shots >= 30 && accuracy >= 0.9);
  check("combo_master", run.maxCombo >= 40);
  check("boss_slayer", run.bosses >= 1);
  check("ufo_hunter", run.ufoKills >= 5);
  check("wrecking_ball", run.uiDestroyed >= 25);
  check("total_annihilation", run.uiDestroyed >= 100);
  check("marathon", run.durationMs >= 10 * 60 * 1000);
  return earned;
}

/**
 * A game with nothing to record: the player never touched the controls and it
 * unlocked nothing new for them. A secret code typed or a date on the calendar
 * still counts - an achievement is only ever saved with its run.
 */
export function isUntouchedRun(engaged: boolean, earned: Iterable<string>, alreadyUnlocked: ReadonlySet<string>): boolean {
  if (engaged) return false;
  for (const id of earned) if (!alreadyUnlocked.has(id)) return false;
  return true;
}
