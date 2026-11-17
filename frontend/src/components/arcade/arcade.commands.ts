/**
 * The waiting arcade's commands. Handlers live in `AppShell`, next to
 * the other shell-level commands; the arcade itself is mounted above the routes.
 */
import { Gamepad2, Trophy } from "lucide-react";
import { defineCommands } from "@/lib/commands/types";

export default defineCommands([
  {
    id: "arcade.play",
    category: "Arcade",
    title: "Play Space Invaders",
    keywords: ["game", "invaders", "arcade", "play", "fun", "break"],
    icon: Gamepad2,
    when: ["arcadeEnabled"],
    handler: "arcade.play",
  },
  {
    id: "arcade.leaderboard",
    category: "Arcade",
    title: "Open the leaderboard",
    keywords: ["scores", "high score", "ranking", "achievements", "invaders"],
    icon: Trophy,
    when: ["arcadeEnabled"],
    handler: "arcade.leaderboard",
  },
]);
