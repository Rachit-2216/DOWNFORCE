export type ToolMode = "replay" | "intelligence" | "strategy";

export function detailedRaceHref(sessionId: string, mode: ToolMode): string {
  return `/app/replay/${encodeURIComponent(sessionId)}?mode=${mode}`;
}

export function replayLibraryHref(mode?: ToolMode): string {
  return mode ? `/app/replays?mode=${mode}` : "/app/replays";
}
