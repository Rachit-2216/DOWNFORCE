import { describe, expect, it, vi } from "vitest";
import { parseTelemetryUrl, telemetryQuery } from "./url-state";
import {
  getTelemetrySessions,
  getTelemetryComparison,
  getTelemetryTrace,
} from "./client";
import type {
  AlignedTrace,
  Comparison,
  TelemetrySelection,
  Trace,
} from "./types";
import {
  nearestIndex,
  presentationIndices,
  RequestEpoch,
  fastestValidLap,
} from "./telemetry-utils";

describe("telemetry URL state", () => {
  it("round trips revision, pair, focused view and zoom", () => {
    const state = parseTelemetryUrl(
      "?session=2024_bahrain_race&revision=" +
        "a".repeat(64) +
        "&driver_a=1&lap_a=4&driver_b=1&lap_b=8&view=inputs&zoom=120,520",
    );
    expect(parseTelemetryUrl(telemetryQuery(state))).toEqual(state);
    expect(state.driverB).toBe("1");
    expect(state.zoom).toEqual([120, 520]);
  });
  it("rejects unbounded or malformed state and supports replay context", () => {
    expect(
      parseTelemetryUrl(
        "?session=../bad&revision=bad&lap_a=9999&driver_a=0&zoom=NaN,4&view=no",
      ),
    ).toEqual({ view: "overview" });
    expect(
      parseTelemetryUrl("?session=test_race&driver=44&lap=9"),
    ).toMatchObject({ driverA: "44", lapA: 9 });
  });
  it("round trips every explicit quality override in shared URLs", () => {
    const state = parseTelemetryUrl(
      "?allow_partial=true&allow_pit_laps=false&allow_deleted=true&allow_provider_generated=true&allow_unreliable=false",
    );
    expect(state).toMatchObject({
      allow_partial: true,
      allow_pit_laps: false,
      allow_deleted: true,
      allow_provider_generated: true,
      allow_unreliable: false,
    });
    expect(parseTelemetryUrl(telemetryQuery(state))).toEqual(state);
    expect(telemetryQuery(state)).toContain("allow_partial=true");
  });
  it("fails closed for malformed or duplicated quality opt-ins", () => {
    expect(
      parseTelemetryUrl(
        "?allow_partial=1&allow_deleted=TRUE&allow_unreliable=no&allow_pit_laps=true&allow_pit_laps=false",
      ),
    ).toMatchObject({
      allow_partial: false,
      allow_deleted: false,
      allow_unreliable: false,
      allow_pit_laps: false,
    });
  });
});
describe("local shared cursor and presentation", () => {
  it("seeks locally without making requests", () => {
    const fetcher = vi.spyOn(globalThis, "fetch");
    expect(nearestIndex([0, 10, 20], 16)).toBe(2);
    expect(nearestIndex([0, 10, 20], -10)).toBe(0);
    expect(fetcher).not.toHaveBeenCalled();
    fetcher.mockRestore();
  });
  it("keeps peaks and categorical transitions in presentation reduction", () => {
    const values = Array.from({ length: 1000 }, () => 10);
    values[505] = 99;
    expect(presentationIndices(values, 30, false)).toContain(505);
    expect(presentationIndices([1, 1, 1, 2, 2, 1], 3, true)).toEqual([
      0, 2, 3, 4, 5,
    ]);
  });
  it("invalidates stale requests even when an aborted fetch resolves", () => {
    const epoch = new RequestEpoch();
    const old = epoch.begin();
    const current = epoch.begin();
    expect(epoch.current(old)).toBe(false);
    expect(epoch.current(current)).toBe(true);
  });
  it("defaults to fastest valid observed non-pit lap", () => {
    const base = {
      reliable: true,
      deleted: false,
      provider_generated: false,
      pit_in: false,
      pit_out: false,
      car_available: true,
      timing_available: true,
    };
    expect(
      fastestValidLap([
        { ...base, lap_number: 2, lap_time_seconds: 82 },
        { ...base, lap_number: 3, lap_time_seconds: 70, deleted: true },
        { ...base, lap_number: 5, lap_time_seconds: 80 },
      ]),
    ).toBe(5);
  });
  it.each([0, -1, NaN, Infinity, -Infinity, null])(
    "never defaults to unusable observed timing %s",
    (time) => {
      const base = {
        reliable: true,
        deleted: false,
        provider_generated: false,
        pit_in: false,
        pit_out: false,
        car_available: true,
        timing_available: true,
      };
      expect(
        fastestValidLap([
          { ...base, lap_number: 1, lap_time_seconds: time },
          { ...base, lap_number: 2, lap_time_seconds: 80 },
        ]),
      ).toBe(2);
      expect(
        fastestValidLap([{ ...base, lap_number: 1, lap_time_seconds: time }]),
      ).toBeUndefined();
    },
  );
  it("never defaults to a lap without available timing", () => {
    const base = {
      reliable: true,
      deleted: false,
      provider_generated: false,
      pit_in: false,
      pit_out: false,
      car_available: true,
      timing_available: true,
    };
    expect(
      fastestValidLap([
        {
          ...base,
          lap_number: 1,
          lap_time_seconds: 70,
          timing_available: false,
        },
        { ...base, lap_number: 2, lap_time_seconds: 80 },
      ]),
    ).toBe(2);
    expect(
      fastestValidLap([{ ...base, lap_number: 1, lap_time_seconds: 0 }]),
    ).toBeUndefined();
  });
});
describe("bounded production client", () => {
  it.each(
    ["aligned A", "aligned B", "trace", "native"].flatMap((location) =>
      ["channel", "mask"].map((missing) => ({ location, missing })),
    ),
  )(
    "rejects unmatched $missing keys in $location data",
    async ({ location, missing }) => {
      const selection: TelemetrySelection = {
        session: "test_race",
        revision: "a".repeat(64),
        driverA: "1",
        driverB: "44",
        lapA: 1,
        lapB: 2,
        view: "track",
      };
      const side = (): AlignedTrace => ({
        driver: "1",
        lap_number: 1,
        elapsed_seconds: [0, 1],
        valid_mask: [true, true],
        channels: { x: [0, 10], y: [0, 20] },
        channel_valid_masks: { x: [true, true], y: [true, true] },
      });
      const comparison: Comparison = {
        session_id: "test_race",
        revision: selection.revision!,
        provenance: "DERIVED",
        algorithm_version: "fixture",
        corner_version: "fixture",
        settings: {},
        distance_m: [0, 100],
        a: side(),
        b: { ...side(), driver: "44", lap_number: 2 },
        delta_seconds: [0, 0.1],
        valid_mask: [true, true],
        end_delta_seconds: 0.1,
        observed_lap_delta_seconds: 0.1,
        finish_line_agreement_applicable: true,
        finish_delta_error_seconds: 0,
        delta_convention: "A-B",
        flags: [],
        availability_a: {},
        availability_b: {},
      };
      const trace: Trace = {
        session_id: "test_race",
        revision: selection.revision!,
        driver: "1",
        lap_number: 1,
        provenance: "DERIVED",
        distance_m: [0, 100],
        elapsed_seconds: [0, 1],
        channels: side().channels,
        channel_valid_masks: side().channel_valid_masks,
        source_samples: {
          provenance: "PROVIDER_NATIVE",
          session_time_ns: [0, 1000000000],
          channels: side().channels,
          channel_valid_masks: side().channel_valid_masks,
          source_sensors: [null, null],
        },
        channel_availability: {},
        context: {
          driver: "1",
          lap_number: 1,
          lap_time_seconds: 1,
          sector_1_seconds: null,
          sector_2_seconds: null,
          sector_3_seconds: null,
          compound: null,
          stint: null,
          tyre_life: null,
          track_status: null,
          qualifying_phase: null,
          reliable: true,
          deleted: false,
          provider_generated: false,
          pit_in: false,
          pit_out: false,
          timing_available: true,
          car_available: true,
          position_available: true,
          weather_available: false,
        },
        flags: [],
        start_covered: true,
        finish_covered: true,
        distance_derived: true,
        clock_source: "fixture",
        brake_label: "BRAKE_ACTIVE",
        drs_interpretation: "UNKNOWN_PROVIDER_CODE",
        position_provenance: "fixture",
      };
      const payload = location.startsWith("aligned") ? comparison : trace;
      const fetcher = vi.fn(async () => new Response(JSON.stringify(payload)));
      const load = () =>
        location.startsWith("aligned")
          ? getTelemetryComparison(selection, {}, { fetcher })
          : getTelemetryTrace(selection, "A", {}, { fetcher });
      await expect(load()).resolves.toEqual(payload);
      const target =
        location === "aligned A"
          ? comparison.a
          : location === "aligned B"
            ? comparison.b
            : location === "native"
              ? trace.source_samples
              : trace;
      if (missing === "channel") delete target.channels.x;
      else delete target.channel_valid_masks.y;
      await expect(load()).rejects.toThrow("invalid or unbounded response");
    },
  );
  it("loads production sessions and passes cancellation", async () => {
    const controller = new AbortController();
    const fetcher = vi.fn<typeof fetch>(
      async () =>
        new Response(
          JSON.stringify({
            items: [],
            total: 0,
            offset: 0,
            limit: 100,
            source: "production-canonical",
            root_configured: true,
          }),
        ),
    );
    expect(
      (await getTelemetrySessions({}, { fetcher, signal: controller.signal }))
        .source,
    ).toBe("production-canonical");
    expect(fetcher.mock.calls[0]?.[1]).toMatchObject({
      signal: controller.signal,
    });
  });
  it.each([
    { replay_session_id: "2024-08-race" },
    { race_analytics_session_id: "../../race" },
    { replay_driver_numbers: [44] },
    { replay_laps_by_driver: { "44": [0] } },
  ])("rejects malformed destination metadata %j", async (context) => {
    const fetcher = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            items: [
              {
                session_id: "2024-08-race",
                revision: "a".repeat(64),
                season: 2024,
                session_type: "race",
                event: "Monaco",
                circuit: null,
                drivers: ["44"],
                available_tables: [],
                channel_availability: {},
                ...context,
              },
            ],
            total: 1,
            offset: 0,
            limit: 100,
            source: "production-canonical",
            root_configured: true,
          }),
        ),
    );
    await expect(getTelemetrySessions({}, { fetcher })).rejects.toThrow(
      "invalid",
    );
  });
  it("rejects nonproduction and oversized responses", async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            items: [],
            total: 0,
            offset: 0,
            limit: 100,
            source: "demo",
          }),
        ),
    );
    await expect(getTelemetrySessions({}, { fetcher })).rejects.toThrow(
      "invalid",
    );
  });
  it("surfaces truthful backend failure details", async () => {
    const fetcher = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            error: {
              code: "all_zero_source",
              message: "All source values are zero",
            },
          }),
          { status: 409 },
        ),
    );
    await expect(
      getTelemetryComparison(
        {
          session: "test_race",
          revision: "a".repeat(64),
          driverA: "1",
          driverB: "44",
          lapA: 1,
          lapB: 2,
          view: "overview",
        },
        {},
        { fetcher },
      ),
    ).rejects.toThrow("All source values are zero");
  });
});
