/**
 * The waiting arcade's scoreboard: who is on top today, this week and
 * forever, who is playing right now, the hall of fame, and the player's own
 * profile (callsign, public-board opt-out, stats, achievements).
 *
 * `ArcadeLeaderboardPanel` is the board alone - the game's pause and game-over
 * screens embed it; the default export is the full dialog (palette command,
 * user menu, waiting panel, game-over screen).
 */
import { useEffect, useMemo, useState } from "react";
import { Crosshair, Crown, Gamepad2, Hammer, Lock, Medal, Timer, Trophy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { SegmentedFilter, type SegmentedFilterOption } from "@/components/ui/segmented-filter";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useArcadeLeaderboard, useArcadeProfile, useUpdateArcadeProfile } from "@/hooks/api/useArcadeApi";
import { useFormatter } from "@/i18n";
import { REFETCH_INTERVAL_STANDARD } from "@/lib/constants";
import { cn, formatDurationShort } from "@/lib/utils";
import type { ArcadeLeaderboardEntry, ArcadePeriod, ArcadeTitleHolder } from "@/types";
import { ACHIEVEMENT_CATEGORY_ICONS, PERIOD_OPTIONS } from "./display";
import { ACHIEVEMENTS } from "./engine/achievements";
import { Spinner } from "@filigran/design-system";

function RankBadge({ rank }: { rank: number }) {
  if (rank === 1) return <Crown className="h-4 w-4 text-feedback-warning-primary light:text-feedback-warning-primary" aria-label="1" />;
  if (rank === 2) return <Medal className="h-4 w-4 text-default-secondary light:text-default-secondary" aria-label="2" />;
  if (rank === 3) return <Medal className="h-4 w-4 text-feedback-warning-primary light:text-feedback-warning-primary" aria-label="3" />;
  return <span className="font-mono-plex content-compact text-default-secondary">{rank}</span>;
}

function EntryRow({
  entry,
  compact,
  tooltipContainer,
}: {
  entry: ArcadeLeaderboardEntry;
  compact: boolean;
  tooltipContainer?: HTMLElement | null;
}) {
  const { t_i18n, intl } = useFormatter();
  return (
    <li
      className={cn(
        "grid items-center gap-3 rounded-sm px-3 py-2 content-base",
        compact ? "grid-cols-[2rem_1fr_auto]" : "grid-cols-[2rem_1fr_auto_3rem_3.5rem_4.5rem]",
        entry.is_self ? "bg-accent/10 ring-1 ring-accent/30" : "hover:bg-elevation-hover",
      )}
    >
      <span className="flex justify-center">
        <RankBadge rank={entry.rank} />
      </span>
      <span className="min-w-0 truncate">
        <span className="font-mono-plex content-base font-bold tracking-wider text-accent">{entry.callsign}</span>
        {entry.is_self && <span className="ml-1.5 content-compact text-accent">{t_i18n("(you)")}</span>}
      </span>
      <span className="text-right font-mono-plex font-semibold tabular-nums">{intl.formatNumber(entry.best_score)}</span>
      {!compact && (
        <>
          <Tip container={tooltipContainer} content={t_i18n("Best wave")}>
            <span className="text-right font-mono-plex content-compact text-default-secondary">W{entry.best_wave}</span>
          </Tip>
          <Tip container={tooltipContainer} content={t_i18n("Games played")}>
            <span className="text-right font-mono-plex content-compact text-default-secondary">{entry.games}</span>
          </Tip>
          <Tip container={tooltipContainer} content={t_i18n("Interface elements destroyed")}>
            <span className="flex items-center justify-end gap-1 font-mono-plex content-compact text-default-secondary">
              <Hammer className="h-3 w-3" />
              {entry.ui_destroyed}
            </span>
          </Tip>
        </>
      )}
    </li>
  );
}

function TitleCard({
  icon: Icon,
  label,
  holder,
  value,
}: {
  icon: typeof Trophy;
  label: string;
  holder: ArcadeTitleHolder | null;
  value: (holder: ArcadeTitleHolder) => string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <div className="rounded-sm border border-elevation-subtle-soft layer-1 bg-elevation-default p-3">
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-default-secondary">
        <Icon className="h-3.5 w-3.5 text-accent" />
        {t_i18n(label)}
      </div>
      {holder ? (
        <div className="mt-1.5 flex items-baseline justify-between gap-2">
          <span className="min-w-0 truncate content-base">
            <span className="font-mono-plex font-bold text-accent">{holder.callsign}</span>
            {holder.is_self && <span className="ml-1.5 content-compact text-accent">{t_i18n("(you)")}</span>}
          </span>
          <span className="shrink-0 font-mono-plex content-base font-semibold">{value(holder)}</span>
        </div>
      ) : (
        <p className="mt-1.5 content-compact text-default-secondary">{t_i18n("Up for grabs")}</p>
      )}
    </div>
  );
}

export function ArcadeLeaderboardPanel({
  compact = false,
  initialPeriod = "week",
  onPlay,
  tooltipContainer,
}: {
  compact?: boolean;
  initialPeriod?: ArcadePeriod;
  onPlay?: () => void;
  /** Portal target of the board's tooltips when it is shown inside the game. */
  tooltipContainer?: HTMLElement | null;
}) {
  const { t_i18n, intl } = useFormatter();
  const [period, setPeriod] = useState<ArcadePeriod>(initialPeriod);
  const { data, isLoading, isError } = useArcadeLeaderboard(period, {
    refetchInterval: REFETCH_INTERVAL_STANDARD,
    limit: compact ? 8 : 20,
  });
  const entries = data?.entries ?? [];
  const me = data?.me ?? null;
  const meOutsideTop = me && !entries.some((entry) => entry.is_self);
  const others = (data?.live ?? []).filter((player) => !player.is_self);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SegmentedFilter options={PERIOD_OPTIONS} value={period} onChange={setPeriod} ariaLabel={t_i18n("Leaderboard period")} />
        {data && (
          <span className="content-compact text-default-secondary">
            {t_i18n("{count, plural, one {# player} other {# players}}", { count: data.total_players })}
          </span>
        )}
      </div>

      {others.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-sm border border-accent/20 bg-accent/[0.04] px-3 py-2">
          <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-accent">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden />
            {t_i18n("Playing now")}
          </span>
          {others.slice(0, 6).map((player, index) => (
            <span key={`${player.callsign}-${index}`} className="rounded-sm bg-elevation-hover px-2 py-0.5 font-mono-plex content-compact">
              <span className="font-bold text-accent">{player.callsign}</span> {intl.formatNumber(player.score)} · W{player.wave}
            </span>
          ))}
        </div>
      )}

      {!compact && data && (
        <div className="grid gap-2 sm:grid-cols-3">
          <TitleCard
            icon={Hammer}
            label="Top vandal"
            holder={data.titles.top_vandal}
            value={(h) => t_i18n("{count} destroyed", { count: intl.formatNumber(Math.round(h.value)) })}
          />
          <TitleCard
            icon={Crosshair}
            label="Sharpshooter"
            holder={data.titles.sharpshooter}
            value={(h) => intl.formatNumber(h.value, { style: "percent", maximumFractionDigits: 1 })}
          />
          <TitleCard
            icon={Timer}
            label="Marathon"
            holder={data.titles.marathon}
            value={(h) => formatDurationShort(h.value / 1000)}
          />
        </div>
      )}

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: compact ? 4 : 6 }).map((_, i) => (
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      ) : isError ? (
        <p className="rounded-sm border border-dashed border-elevation-subtle p-4 text-center content-base text-default-secondary">
          {t_i18n("The leaderboard is unavailable right now.")}
        </p>
      ) : entries.length === 0 ? (
        <div className="rounded-sm border border-dashed border-elevation-subtle p-6 text-center">
          <Gamepad2 className="mx-auto h-8 w-8 text-accent/60" />
          <p className="mt-2 content-base text-default-secondary">{t_i18n("No scores yet for this period. The board is yours to take.")}</p>
          {onPlay && (
            <Button size="sm" className="mt-3" onClick={onPlay}>
              {t_i18n("Play now")}
            </Button>
          )}
        </div>
      ) : (
        <ol className="space-y-1">
          {entries.map((entry) => (
            <EntryRow key={entry.rank} entry={entry} compact={compact} tooltipContainer={tooltipContainer} />
          ))}
          {meOutsideTop && me && (
            <>
              <li className="py-0.5 text-center content-compact text-default-secondary" aria-hidden>
                ···
              </li>
              <EntryRow entry={me} compact={compact} tooltipContainer={tooltipContainer} />
            </>
          )}
        </ol>
      )}
    </div>
  );
}

function ProfileTab() {
  const { t_i18n, intl } = useFormatter();
  const { data: profile, isLoading } = useArcadeProfile();
  const update = useUpdateArcadeProfile();
  const [callsign, setCallsign] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (profile) setCallsign(profile.callsign ?? "");
  }, [profile]);

  if (isLoading || !profile) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  const save = (value: string | null) => {
    setError(null);
    update.mutate(
      { callsign: value },
      { onError: (e) => setError(e.message || t_i18n("That callsign could not be saved.")) },
    );
  };

  const stats: [string, string][] = [
    ["Games played", intl.formatNumber(profile.stats.games)],
    ["Best score", intl.formatNumber(profile.stats.best_score)],
    ["Best wave", intl.formatNumber(profile.stats.best_wave)],
    ["Invaders destroyed", intl.formatNumber(profile.stats.total_kills)],
    ["Interface elements destroyed", intl.formatNumber(profile.stats.total_ui_destroyed)],
    ["Bosses defeated", intl.formatNumber(profile.stats.bosses)],
    ["Time played", formatDurationShort(profile.stats.total_play_ms / 1000)],
  ];

  return (
    <div className="space-y-5">
      <div className="space-y-2">
        <label htmlFor="arcade-callsign" className="content-base font-medium">
          {t_i18n("Arcade callsign")}
        </label>
        <p className="content-compact text-default-secondary">
          {t_i18n("Three letters or digits, like on a real cabinet. Leave it empty to use your initials ({initials}).", {
            initials: profile.default_callsign,
          })}
        </p>
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            save(callsign.trim() || null);
          }}
        >
          <Input aria-label={t_i18n("Arcade callsign")}
            id="arcade-callsign"
            value={callsign}
            maxLength={3}
            placeholder={profile.default_callsign}
            onChange={(event) => setCallsign(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""))}
            className="w-24 text-center font-mono-plex text-lg font-bold tracking-[0.3em] uppercase"
            aria-invalid={!!error}
          />
          <Button type="submit" size="sm" disabled={update.isPending || callsign === (profile.callsign ?? "")} aria-busy={update.isPending || undefined}>
            {update.isPending && <Spinner size="sm" tone="inherit" />}
            {t_i18n("Save")}
          </Button>
          {profile.callsign && (
            <Button type="button" size="sm" variant="ghost" onClick={() => save(null)} disabled={update.isPending}>
              {t_i18n("Use my initials")}
            </Button>
          )}
        </form>
        {error && <p className="content-compact text-feedback-error-primary">{error}</p>}
      </div>

      <div className="flex items-start justify-between gap-4 rounded-sm border border-elevation-subtle-soft p-3">
        <div>
          <p className="content-base font-medium">{t_i18n("Show me on the public leaderboard")}</p>
          <p className="content-compact text-default-secondary">
            {t_i18n("When off, other players do not see your scores, titles or live games. You still see your own rank.")}
          </p>
        </div>
        <Switch
          checked={!profile.hidden}
          onCheckedChange={(visible) => {
            setError(null);
            // Mutations here skip the global error toast: say it where it happened.
            update.mutate(
              { hidden: !visible },
              { onError: (e) => setError(e.message || t_i18n("Failed to save")) },
            );
          }}
          disabled={update.isPending}
          aria-label={t_i18n("Show me on the public leaderboard")}
        />
      </div>

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {stats.map(([label, value]) => (
          <div key={label} className="rounded-sm bg-elevation-hover p-3">
            <dt className="text-[11px] text-default-secondary">{t_i18n(label)}</dt>
            <dd className="mt-0.5 font-mono-plex text-base font-semibold">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function AchievementsTab() {
  const { t_i18n } = useFormatter();
  const { data: profile } = useArcadeProfile();
  const unlocked = profile?.achievements ?? {};
  const count = ACHIEVEMENTS.filter((a) => a.id in unlocked).length;
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="content-base text-default-secondary">
          {t_i18n("{unlocked} of {total} unlocked", { unlocked: count, total: ACHIEVEMENTS.length })}
        </p>
        <div className="h-1.5 w-40 overflow-hidden rounded-full bg-elevation-hover">
          <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${(count / ACHIEVEMENTS.length) * 100}%` }} />
        </div>
      </div>
      <ul className="grid gap-2 sm:grid-cols-2">
        {ACHIEVEMENTS.map((achievement) => {
          const done = achievement.id in unlocked;
          const hidden = achievement.secret && !done;
          const Icon = hidden ? Lock : ACHIEVEMENT_CATEGORY_ICONS[achievement.category];
          return (
            <li
              key={achievement.id}
              className={cn(
                "flex items-start gap-3 rounded-sm border p-3",
                done ? "border-accent/30 bg-accent/[0.05]" : "border-elevation-subtle-soft opacity-60",
              )}
            >
              <span
                className={cn(
                  "flex h-8 w-8 shrink-0 items-center justify-center rounded-sm",
                  done ? "bg-accent/15 text-accent" : "bg-elevation-hover text-default-secondary",
                )}
              >
                <Icon className="h-4 w-4" />
              </span>
              <span className="min-w-0">
                <span className="block content-base font-medium">{hidden ? "???" : t_i18n(achievement.title)}</span>
                <span className="block content-compact text-default-secondary">
                  {hidden ? t_i18n("A secret achievement. Keep playing.") : t_i18n(achievement.description)}
                </span>
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

type Tab = "board" | "profile" | "achievements";

const TAB_OPTIONS: SegmentedFilterOption<Tab>[] = [
  { key: "board", label: "Leaderboard" },
  { key: "profile", label: "My arcade" },
  { key: "achievements", label: "Achievements" },
];

export default function ArcadeLeaderboardDialog({
  open,
  onOpenChange,
  onPlay,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onPlay?: () => void;
}) {
  const { t_i18n } = useFormatter();
  const [tab, setTab] = useState<Tab>("board");
  const play = useMemo(
    () =>
      onPlay
        ? () => {
            onOpenChange(false);
            onPlay();
          }
        : undefined,
    [onOpenChange, onPlay],
  );
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-sans-geologica">
            <Trophy className="h-5 w-5 text-accent" />
            {t_i18n("Arcade")}
          </DialogTitle>
          <DialogDescription>
            {t_i18n("Space Invaders scores across the platform. Play any time, or take control of the waiting game while an agent works, to climb the board.")}
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <SegmentedFilter options={TAB_OPTIONS} value={tab} onChange={setTab} ariaLabel={t_i18n("Arcade sections")} />
          {play && (
            <Button size="sm" onClick={play}>
              <Gamepad2 className="mr-1.5 h-4 w-4" />
              {t_i18n("Play now")}
            </Button>
          )}
        </div>
        {tab === "board" && <ArcadeLeaderboardPanel onPlay={play} />}
        {tab === "profile" && <ProfileTab />}
        {tab === "achievements" && <AchievementsTab />}
      </DialogContent>
    </Dialog>
  );
}
