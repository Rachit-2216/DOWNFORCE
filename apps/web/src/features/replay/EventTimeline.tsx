import { useMemo, type CSSProperties } from "react";

import type { Driver, TimelineEvent } from "@/lib/api/types";

import {
  eventLabel,
  formatClock,
  isSignificantTimelineEvent,
  referenceLapMarkers,
} from "./replay-utils";

type Props = {
  events: TimelineEvent[];
  minimumMs: number;
  maximumMs: number;
  cursorMs: number;
  drivers?: Driver[];
  onSeek: (timeMs: number) => void;
};

function markerKind(event: TimelineEvent): string {
  if (
    event.event_type === "driver-pit-entered" ||
    event.event_type === "driver-pit-exited"
  )
    return "pit";
  if (event.event_type === "track-status-changed") return "status";
  if (event.event_type === "race-control-event") return "control";
  if (event.event_type === "weather-observed") return "weather";
  if (event.event_type === "driver-status-changed") return "driver";
  return "neutral";
}

const MARKER_PRIORITY: Record<string, number> = {
  "track-status-changed": 0,
  "driver-status-changed": 1,
  "driver-pit-entered": 2,
  "driver-pit-exited": 3,
  "race-control-event": 4,
  "weather-observed": 5,
  "driver-stint-changed": 6,
  "session-marker": 7,
  "driver-position-changed": 8,
  "driver-lap-completed": 9,
};
const TIMELINE_BIN_COUNT = 40;

function markerPriority(event: TimelineEvent): number {
  return MARKER_PRIORITY[event.event_type] ?? 99;
}

export function EventTimeline({
  events,
  minimumMs,
  maximumMs,
  cursorMs,
  drivers = [],
  onSeek,
}: Props) {
  const duration = Math.max(1, maximumMs - minimumMs);
  const driverNames = useMemo(
    () =>
      new Map(
        drivers.map((driver) => [
          driver.driver_id,
          driver.abbreviation ??
            driver.full_name ??
            `#${driver.racing_number ?? "?"}`,
        ]),
      ),
    [drivers],
  );
  const { significant, displayed, lapMarkers } = useMemo(() => {
    const selected = events.filter(isSignificantTimelineEvent);
    const bins = new Map<number, TimelineEvent[]>();
    selected.forEach((event) => {
      const bin = Math.round(
        ((event.session_time_ms - minimumMs) / duration) * TIMELINE_BIN_COUNT,
      );
      bins.set(bin, [...(bins.get(bin) ?? []), event]);
    });
    return {
      significant: selected,
      displayed: [...bins.entries()].map(([bin, eventsInBin]) => ({
        event: [...eventsInBin].sort(
          (left, right) =>
            markerPriority(left) - markerPriority(right) ||
            left.session_time_ms - right.session_time_ms,
        )[0],
        count: eventsInBin.length,
        positionPercent: (bin / TIMELINE_BIN_COUNT) * 100,
      })),
      lapMarkers: referenceLapMarkers(events),
    };
  }, [duration, events, minimumMs]);
  const cursorLeft = ((cursorMs - minimumMs) / duration) * 100;

  return (
    <section className="event-timeline" aria-labelledby="event-timeline-title">
      <header>
        <div>
          <span className="panel-index">06</span>
          <h2 id="event-timeline-title">Race events</h2>
        </div>
        <span>{significant.length} significant events</span>
      </header>
      <div className="event-timeline__rail">
        <div
          className="event-timeline__cursor"
          style={{ left: `${cursorLeft}%` }}
        >
          <span>{formatClock(cursorMs)}</span>
        </div>
        {lapMarkers.map((marker, index) => {
          const left = ((marker.sessionTimeMs - minimumMs) / duration) * 100;
          const showLabel =
            marker.lapNumber === 1 ||
            marker.lapNumber % 5 === 0 ||
            index === lapMarkers.length - 1;
          return (
            <span
              className="lap-marker"
              style={{ "--lap-left": `${left}%` } as CSSProperties}
              title={`Reference lap ${marker.lapNumber} · ${formatClock(marker.sessionTimeMs)}`}
              aria-hidden="true"
              key={marker.lapNumber}
            >
              {showLabel ? marker.lapNumber : ""}
            </span>
          );
        })}
        {displayed.map(({ event, count, positionPercent }) => {
          const nearby = count > 1 ? ` · ${count - 1} nearby` : "";
          const driver = event.driver_id
            ? driverNames.get(event.driver_id)
            : undefined;
          const driverDetail = driver ? ` · ${driver}` : "";
          return (
            <button
              type="button"
              className={`event-marker event-marker--${markerKind(event)}`}
              style={
                {
                  "--event-left": `${positionPercent}%`,
                } as CSSProperties
              }
              onClick={() => onSeek(event.session_time_ms)}
              title={`${formatClock(event.session_time_ms)} · ${eventLabel(event)}${driverDetail}${nearby}`}
              aria-label={`Seek to ${formatClock(event.session_time_ms)}: ${eventLabel(event)}${driverDetail}${nearby}`}
              key={event.event_id}
            >
              <span />
            </button>
          );
        })}
      </div>
      <div className="event-timeline__legend">
        <span>
          <i className="legend-dot legend-dot--pit" />
          Pit
        </span>
        <span>
          <i className="legend-dot legend-dot--status" />
          Track status
        </span>
        <span>
          <i className="legend-dot legend-dot--control" />
          Race control
        </span>
        <span>
          <i className="legend-dot legend-dot--weather" />
          Weather
        </span>
      </div>
    </section>
  );
}
