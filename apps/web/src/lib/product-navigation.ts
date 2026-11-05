export type ToolMode = "replay" | "intelligence" | "strategy";

export function detailedRaceHref(
  sessionId: string,
  mode: ToolMode,
  context?: { driverId?: string; timeMs?: number },
): string {
  const query = new URLSearchParams({ mode });
  if (context?.driverId) query.set("driver", context.driverId);
  if (context?.timeMs !== undefined && Number.isFinite(context.timeMs))
    query.set("t", String(Math.max(0, Math.round(context.timeMs))));
  return `/app/replay/${encodeURIComponent(sessionId)}?${query}`;
}

export function replayLibraryHref(mode?: ToolMode): string {
  return mode ? `/app/replays?mode=${mode}` : "/app/replays";
}
