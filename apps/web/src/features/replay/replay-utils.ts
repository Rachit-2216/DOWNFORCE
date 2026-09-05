import type {
  DriverState,
  TimelineEvent,
  TrackPosition,
} from "@/lib/api/types";

export function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function formatClock(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1_000));
  const hours = Math.floor(totalSeconds / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function formatLapTime(milliseconds: number | null): string {
  if (milliseconds === null) return "—";
  const minutes = Math.floor(milliseconds / 60_000);
  const seconds = (milliseconds % 60_000) / 1_000;
  return `${minutes}:${seconds.toFixed(3).padStart(6, "0")}`;
}

export function formatMetric(
  value: number | null,
  suffix: string,
  digits = 1,
): string {
  return value === null ? "—" : `${value.toFixed(digits)}${suffix}`;
}

export function driverLabel(
  driver: Pick<DriverState, "abbreviation" | "full_name" | "racing_number">,
): string {
  return (
    driver.abbreviation ??
    driver.full_name ??
    (driver.racing_number === null ? "Unknown" : `#${driver.racing_number}`)
  );
}

export function sortDrivers(drivers: DriverState[]): DriverState[] {
  return [...drivers].sort((left, right) => {
    if (left.position === null && right.position === null)
      return driverLabel(left).localeCompare(driverLabel(right));
    if (left.position === null) return 1;
    if (right.position === null) return -1;
    return left.position - right.position;
  });
}

export function eventLabel(event: TimelineEvent): string {
  const message = event.payload.message;
  if (typeof message === "string" && message.trim()) return message;
  switch (event.event_type) {
    case "session-marker":
      return "Session marker";
    case "track-status-changed":
      return `Track ${String(event.payload.track_status ?? "status")}`;
    case "weather-observed":
      return event.payload.rainfall === true
        ? "Rain observed"
        : "Weather update";
    case "race-control-event":
      return "Race control";
    case "driver-stint-changed":
      return `Stint ${String(event.payload.stint_number ?? "change")}`;
    case "driver-pit-entered":
      return `Pit entry · lap ${String(event.payload.lap_number ?? "—")}`;
    case "driver-position-changed":
      return `Position ${String(event.payload.position ?? "—")}`;
    case "driver-lap-completed":
      return `Lap ${String(event.payload.lap_number ?? "—")} completed`;
    case "driver-pit-exited":
      return `Pit exit · lap ${String(event.payload.lap_number ?? "—")}`;
    case "driver-status-changed":
      return `Status ${String(event.payload.status ?? "changed")}`;
    default:
      return "Unknown event";
  }
}

export function isSignificantTimelineEvent(event: TimelineEvent): boolean {
  if (
    [
      "driver-position-changed",
      "driver-lap-completed",
      "weather-observed",
    ].includes(event.event_type)
  ) {
    return (
      event.event_type === "weather-observed" && event.payload.rainfall === true
    );
  }
  return event.event_type !== "session-marker";
}

export type ReferenceLapMarker = {
  lapNumber: number;
  sessionTimeMs: number;
};

export function referenceLapMarkers(
  events: TimelineEvent[],
): ReferenceLapMarker[] {
  const earliestLeaderObservation = new Map<number, number>();
  events.forEach((event) => {
    if (
      event.event_type !== "driver-position-changed" ||
      event.payload.position !== 1 ||
      typeof event.payload.lap_number !== "number"
    )
      return;
    const lapNumber = event.payload.lap_number;
    const current = earliestLeaderObservation.get(lapNumber);
    if (current === undefined || event.session_time_ms < current)
      earliestLeaderObservation.set(lapNumber, event.session_time_ms);
  });
  return [...earliestLeaderObservation]
    .sort(([left], [right]) => left - right)
    .map(([lapNumber, sessionTimeMs]) => ({ lapNumber, sessionTimeMs }));
}

export type TrackProjection = {
  width: number;
  height: number;
  path: string;
  project: (point: Pick<TrackPosition, "x_m" | "y_m">) => {
    x: number;
    y: number;
  };
};

export function longestDriverTrace(
  positions: TrackPosition[],
): TrackPosition[] {
  const byDriver = new Map<string, TrackPosition[]>();
  positions.forEach((point) => {
    if (!Number.isFinite(point.x_m) || !Number.isFinite(point.y_m)) return;
    byDriver.set(point.driver_id, [
      ...(byDriver.get(point.driver_id) ?? []),
      point,
    ]);
  });
  return (
    [...byDriver.values()]
      .filter((trace) => trace.length >= 2)
      .sort(
        (left, right) =>
          right.length - left.length ||
          left[0].driver_id.localeCompare(right[0].driver_id),
      )[0]
      ?.sort((left, right) => left.session_time_ms - right.session_time_ms) ??
    []
  );
}

export function createTrackProjection(
  positions: TrackPosition[],
  width = 720,
  height = 460,
  padding = 42,
): TrackProjection | null {
  const validPositions = positions.filter(
    (point) => Number.isFinite(point.x_m) && Number.isFinite(point.y_m),
  );
  if (validPositions.length < 2) return null;
  const xValues = validPositions.map((point) => point.x_m);
  const yValues = validPositions.map((point) => point.y_m);
  const minX = Math.min(...xValues);
  const maxX = Math.max(...xValues);
  const minY = Math.min(...yValues);
  const maxY = Math.max(...yValues);
  const rangeX = maxX - minX;
  const rangeY = maxY - minY;
  if (rangeX <= 0 || rangeY <= 0) return null;

  const scale = Math.min(
    (width - padding * 2) / rangeX,
    (height - padding * 2) / rangeY,
  );
  const renderedWidth = rangeX * scale;
  const renderedHeight = rangeY * scale;
  const offsetX = (width - renderedWidth) / 2;
  const offsetY = (height - renderedHeight) / 2;
  const project = (point: Pick<TrackPosition, "x_m" | "y_m">) => ({
    x: offsetX + (point.x_m - minX) * scale,
    y: height - (offsetY + (point.y_m - minY) * scale),
  });

  const stride = Math.max(1, Math.ceil(validPositions.length / 700));
  const sampled = validPositions.filter(
    (_, index) => index % stride === 0 || index === validPositions.length - 1,
  );
  const path = sampled
    .map((point, index) => {
      const projected = project(point);
      return `${index === 0 ? "M" : "L"}${projected.x.toFixed(1)},${projected.y.toFixed(1)}`;
    })
    .join(" ");
  return { width, height, path, project };
}

export function latestPositionsAt(
  positions: TrackPosition[],
  cursorMs: number,
  staleAfterMs = 5_000,
  maximumPresentationSpeedMps = 150,
): Map<string, TrackPosition> {
  const latest = new Map<string, TrackPosition>();
  [...positions]
    .sort((left, right) => left.session_time_ms - right.session_time_ms)
    .forEach((point) => {
      if (!Number.isFinite(point.x_m) || !Number.isFinite(point.y_m)) return;
      if (
        point.session_time_ms > cursorMs ||
        cursorMs - point.session_time_ms > staleAfterMs
      )
        return;
      const current = latest.get(point.driver_id);
      if (current && current.session_time_ms < point.session_time_ms) {
        const elapsedSeconds =
          (point.session_time_ms - current.session_time_ms) / 1_000;
        const distanceMetres = Math.hypot(
          point.x_m - current.x_m,
          point.y_m - current.y_m,
        );
        if (distanceMetres / elapsedSeconds > maximumPresentationSpeedMps)
          return;
      }
      if (!current || current.session_time_ms <= point.session_time_ms)
        latest.set(point.driver_id, point);
    });
  return latest;
}
