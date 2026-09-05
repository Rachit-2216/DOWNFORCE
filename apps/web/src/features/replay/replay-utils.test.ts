import { describe, expect, it } from "vitest";

import type {
  DriverState,
  TimelineEvent,
  TrackPosition,
} from "@/lib/api/types";

import {
  clamp,
  createTrackProjection,
  eventLabel,
  formatClock,
  formatLapTime,
  latestPositionsAt,
  longestDriverTrace,
  referenceLapMarkers,
  sortDrivers,
} from "./replay-utils";

const driver = (
  position: number | null,
  abbreviation: string,
): DriverState => ({
  driver_id: abbreviation,
  racing_number: null,
  abbreviation,
  full_name: abbreviation,
  team_name: null,
  status: "active",
  position,
  laps_completed: 1,
  current_stint: 1,
  compound: "medium",
  tyre_age_laps: 1,
  last_lap_time_ms: null,
  in_pit: false,
  pit_stop_count: 0,
  last_pit_lap: null,
});

const point = (
  driverId: string,
  time: number,
  x: number,
  y: number,
): TrackPosition => ({
  driver_id: driverId,
  session_time_ms: time,
  x_m: x,
  y_m: y,
  z_m: null,
  raw_status: null,
});

describe("replay utilities", () => {
  it("clamps cursors and formats race time without timezone semantics", () => {
    expect(clamp(12, 20, 30)).toBe(20);
    expect(formatClock(3_661_900)).toBe("01:01:01");
    expect(formatLapTime(91_773)).toBe("1:31.773");
  });

  it("orders classified drivers before unavailable positions", () => {
    expect(
      sortDrivers([
        driver(null, "GAS"),
        driver(2, "HAM"),
        driver(1, "RUS"),
      ]).map((item) => item.abbreviation),
    ).toEqual(["RUS", "HAM", "GAS"]);
  });

  it("projects real coordinates with preserved aspect ratio", () => {
    const projection = createTrackProjection(
      [point("a", 0, 0, 0), point("a", 1, 100, 50)],
      200,
      100,
      10,
    );
    expect(projection).not.toBeNull();
    expect(projection?.path).toMatch(/^M/);
    const start = projection?.project({ x_m: 0, y_m: 0 });
    const end = projection?.project({ x_m: 100, y_m: 50 });
    expect(start?.x).toBeGreaterThanOrEqual(10);
    expect(end?.x).toBeLessThanOrEqual(190);
  });

  it("rejects an unusable circuit trace instead of rendering NaN coordinates", () => {
    expect(
      createTrackProjection([
        point("a", 0, Number.NaN, 0),
        point("a", 1, 10, 10),
      ]),
    ).toBeNull();
  });

  it("selects a usable canonical driver trace when another driver has no samples", () => {
    expect(
      longestDriverTrace([
        point("missing", 0, 0, 0),
        point("available", 2, 20, 10),
        point("available", 1, 10, 5),
      ]).map((sample) => sample.session_time_ms),
    ).toEqual([1, 2]);
  });

  it("selects only the latest causal and non-stale position per driver", () => {
    const latest = latestPositionsAt(
      [
        point("a", 8_000, 1, 1),
        point("a", 9_000, 2, 2),
        point("a", 10_001, 3, 3),
        point("b", 4_000, 4, 4),
      ],
      10_000,
      5_000,
    );
    expect(latest.get("a")?.session_time_ms).toBe(9_000);
    expect(latest.has("b")).toBe(false);
  });

  it("holds the last plausible sample across a spatial discontinuity", () => {
    const latest = latestPositionsAt(
      [point("a", 9_000, 0, 0), point("a", 9_500, 1_000, 0)],
      10_000,
    );
    expect(latest.get("a")?.session_time_ms).toBe(9_000);
  });

  it("derives deterministic P1-reference lap ticks from canonical events", () => {
    const leaderEvent = (
      lapNumber: number,
      sessionTimeMs: number,
    ): TimelineEvent => ({
      event_id: `lap-${lapNumber}-${sessionTimeMs}`,
      session_time_ms: sessionTimeMs,
      priority: 50,
      sequence: lapNumber,
      event_type: "driver-position-changed",
      driver_id: "driver-a",
      source: "canonical",
      source_key: null,
      payload: { lap_number: lapNumber, position: 1 },
    });
    expect(
      referenceLapMarkers([
        leaderEvent(2, 20_000),
        leaderEvent(1, 10_000),
        leaderEvent(2, 21_000),
      ]),
    ).toEqual([
      { lapNumber: 1, sessionTimeMs: 10_000 },
      { lapNumber: 2, sessionTimeMs: 20_000 },
    ]);
  });

  it("labels a forward-compatible unknown event truthfully", () => {
    expect(
      eventLabel({
        event_id: "future-event",
        session_time_ms: 5_000,
        priority: 99,
        sequence: 1,
        event_type: "future-canonical-event",
        driver_id: null,
        source: "canonical",
        source_key: null,
        payload: {},
      }),
    ).toBe("Unknown event");
  });
});
