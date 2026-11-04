import {
  render,
  screen,
  waitFor,
  fireEvent,
  cleanup,
  act,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TelemetryWorkspace } from "./TelemetryWorkspace";
import { TelemetryCharts } from "./TelemetryCharts";
import * as client from "./client";
import type { Comparison, Trace, TelemetrySession } from "./types";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});
it.each<{
  name: string;
  context: Partial<TelemetrySession>;
  expectedReplay: string | null;
  sessionType: string;
}>([
  {
    name: "missing context",
    context: {},
    expectedReplay: null,
    sessionType: "Race",
  },
  {
    name: "verified context",
    context: {
      replay_session_id: "session-2024-round-08-type-race",
      replay_driver_numbers: ["1"],
      replay_laps_by_driver: { "1": [1] },
      race_analytics_session_id: "archive-2024-round-08-race",
    },
    expectedReplay:
      "/app/replay/session-2024-round-08-type-race?driver=1&lap=1",
    sessionType: "Race",
  },
  {
    name: "incompatible driver context",
    context: {
      replay_session_id: "session-2024-round-08-type-race",
      replay_driver_numbers: ["44"],
      replay_laps_by_driver: { "44": [2] },
      race_analytics_session_id: "archive-2024-round-08-race",
    },
    expectedReplay: "/app/replay/session-2024-round-08-type-race",
    sessionType: "Race",
  },
  {
    name: "incompatible lap context",
    context: {
      replay_session_id: "session-2024-round-08-type-race",
      replay_driver_numbers: ["1"],
      replay_laps_by_driver: { "1": [2] },
      race_analytics_session_id: "archive-2024-round-08-race",
    },
    expectedReplay: "/app/replay/session-2024-round-08-type-race?driver=1",
    sessionType: "Race",
  },
  {
    name: "qualifying weekend context",
    context: {
      replay_session_id: null,
      race_analytics_session_id: "archive-2024-round-08-race",
    },
    expectedReplay: null,
    sessionType: "qualifying",
  },
])(
  "keeps shared cursor and truthful links for $name",
  async ({ context, expectedReplay, sessionType }) => {
    const revision = "a".repeat(64);
    const url = (zoom: string, view = "performance") =>
      `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2&view=${view}&zoom=${zoom}`;
    window.history.replaceState(null, "", url("100,300"));
    vi.spyOn(client, "getTelemetrySessions").mockResolvedValue({
      items: [
        {
          session_id: "test_race",
          revision,
          season: 2024,
          event: "Test fixture",
          circuit: null,
          session_type: sessionType,
          drivers: ["1", "44"],
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
    });
    vi.spyOn(client, "getTelemetryLaps").mockImplementation(
      async (_session, driver) => ({
        session_id: "test_race",
        revision,
        driver,
        items: [],
        total: 0,
        offset: 0,
        limit: 100,
      }),
    );
    const side = {
      driver: "1",
      lap_number: 1,
      elapsed_seconds: [0, 1, 2, 3],
      valid_mask: [true, true, true, true],
      channels: {
        speed_kmh: [100, 110, 120, 130],
        x: [0, 10, 20, 30],
        y: [0, 10, 20, 30],
      },
      channel_valid_masks: {
        speed_kmh: [true, true, true, true],
        x: [true, true, true, true],
        y: [true, true, true, true],
      },
    };
    const compare = vi
      .spyOn(client, "getTelemetryComparison")
      .mockResolvedValue({
        session_id: "test_race",
        revision,
        provenance: "DERIVED",
        algorithm_version: "fixture",
        corner_version: "fixture",
        settings: {},
        distance_m: [0, 100, 200, 300],
        a: side,
        b: { ...side, driver: "44", lap_number: 2 },
        delta_seconds: [0, 0.1, 0.2, 0.3],
        valid_mask: [true, true, true, true],
        end_delta_seconds: 0.3,
        observed_lap_delta_seconds: 0.3,
        finish_line_agreement_applicable: true,
        finish_delta_error_seconds: 0,
        delta_convention: "A-B",
        flags: [],
        availability_a: {},
        availability_b: {},
      });
    vi.spyOn(client, "getTelemetryTrace").mockImplementation(
      async (_selection, sideName) =>
        ({
          session_id: "test_race",
          revision,
          driver: sideName === "A" ? "1" : "44",
          lap_number: sideName === "A" ? 1 : 2,
          provenance: "DERIVED",
          distance_m: [0, 100, 200, 300],
          elapsed_seconds: side.elapsed_seconds,
          channels: side.channels,
          channel_valid_masks: side.channel_valid_masks,
          source_samples: {
            provenance: "PROVIDER_NATIVE",
            session_time_ns: [0, 1, 2, 3],
            channels: side.channels,
            channel_valid_masks: side.channel_valid_masks,
            source_sensors: [null, null, null, null],
          },
          channel_availability: {},
          flags: [],
          start_covered: true,
          finish_covered: true,
          distance_derived: true,
          clock_source: "fixture",
          brake_label: "BRAKE_ACTIVE",
          drs_interpretation: "UNKNOWN_PROVIDER_CODE",
          position_provenance: "fixture",
          context: {
            driver: sideName === "A" ? "1" : "44",
            lap_number: sideName === "A" ? 1 : 2,
            lap_time_seconds: 3,
            sector_1_seconds: 1,
            sector_2_seconds: 1,
            sector_3_seconds: 1,
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
        }) as Trace,
    );
    vi.spyOn(client, "getTelemetryCorners").mockRejectedValue(
      new Error("Unavailable fixture corners"),
    );
    vi.spyOn(client, "getTelemetryMicrosectors").mockRejectedValue(
      new Error("Unavailable fixture microsectors"),
    );
    const fetcher = vi.spyOn(globalThis, "fetch");
    const canvas = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockReturnValue(null);
    const { container } = render(<TelemetryWorkspace />);
    await screen.findByRole("slider", { name: "Shared distance cursor" });
    const replayLink = screen.queryByRole("link", { name: "Open replay ↗" });
    if (expectedReplay)
      expect(replayLink).toHaveAttribute("href", expectedReplay);
    else {
      expect(replayLink).not.toBeInTheDocument();
      expect(screen.getByText(/Replay unavailable/)).toBeInTheDocument();
    }
    const analyticsLink = screen.queryByRole("link", {
      name: /race analytics/i,
    });
    if (context.race_analytics_session_id) {
      expect(analyticsLink).toHaveAttribute(
        "href",
        "/app/analytics/races/archive-2024-round-08-race",
      );
      if (sessionType === "qualifying")
        expect(analyticsLink).toHaveTextContent("Weekend race analytics");
    } else {
      expect(analyticsLink).not.toBeInTheDocument();
      expect(
        screen.getByText(/Race analytics unavailable/),
      ).toBeInTheDocument();
    }
    const assertCursor = (value: number, speed: string) => {
      expect(
        screen.getByRole("slider", { name: "Shared distance cursor" }),
      ).toHaveValue(String(value));
      expect(
        container.querySelector(".tl-cursor-title output"),
      ).toHaveTextContent(`${value} m`);
      expect(screen.getByText(speed)).toBeInTheDocument();
    };
    assertCursor(100, "110.0 / 110.0 km/h");
    await waitFor(() => expect(canvas).toHaveBeenCalled());
    const initialDraws = canvas.mock.calls.length;
    fireEvent.change(
      screen.getByRole("slider", { name: "Shared distance cursor" }),
      { target: { value: "300" } },
    );
    assertCursor(300, "130.0 / 130.0 km/h");
    expect(canvas.mock.calls.length).toBe(initialDraws);
    fireEvent.change(screen.getByRole("slider", { name: "Zoom end m" }), {
      target: { value: "200" },
    });
    assertCursor(200, "120.0 / 120.0 km/h");
    act(() => {
      window.history.replaceState(null, "", url("100,150"));
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    assertCursor(150, "120.0 / 120.0 km/h");
    act(() => {
      window.history.replaceState(null, "", url("0,100", "track"));
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(
      container.querySelector(".tl-cursor-title output"),
    ).toHaveTextContent("100 m");
    const track = screen.getByRole("img", {
      name: /Provider-coordinate track map/,
    });
    expect(track.querySelector("g circle")).toHaveAttribute("cx", "430");
    expect(compare).toHaveBeenCalledTimes(1);
    expect(fetcher).not.toHaveBeenCalled();
  },
);
it("shows an honest empty canonical archive, never invented telemetry", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
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
  render(<TelemetryWorkspace />);
  await screen.findByText("No canonical telemetry sessions available");
  expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  expect(screen.queryByText("Sample data")).not.toBeInTheDocument();
});
it("has a normal retry action for failed data loading", async () => {
  const fetcher = vi
    .spyOn(globalThis, "fetch")
    .mockRejectedValue(new Error("offline"));
  render(<TelemetryWorkspace />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
});
it("distinguishes an unconfigured production root from an empty configured archive", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({
        items: [],
        total: 0,
        offset: 0,
        limit: 100,
        source: "production-canonical",
        root_configured: false,
      }),
    ),
  );
  render(<TelemetryWorkspace />);
  await screen.findByText("Production telemetry root is not configured");
});
it("updates all chart readouts from one cursor without HTTP", () => {
  const fetcher = vi.spyOn(globalThis, "fetch");
  const c = {
    distance_m: [0, 10, 20],
    delta_seconds: [0, 0.1, 0.2],
    valid_mask: [true, true, true],
    a: {
      channels: { speed_kmh: [100, 110, 120], rpm: [9000, 10000, 11000] },
      channel_valid_masks: {
        speed_kmh: [true, true, true],
        rpm: [true, true, true],
      },
    },
    b: {
      channels: { speed_kmh: [99, 109, 119], rpm: [9000, 10000, 11000] },
      channel_valid_masks: {
        speed_kmh: [true, true, true],
        rpm: [true, true, true],
      },
    },
    availability_a: {},
    availability_b: {},
  } as unknown as Comparison;
  const canvasContext = vi
    .spyOn(HTMLCanvasElement.prototype, "getContext")
    .mockReturnValue(null);
  const onCursor = vi.fn();
  const { rerender } = render(
    <TelemetryCharts
      comparison={c}
      view="performance"
      cursor={0}
      zoom={[0, 20]}
      onCursor={onCursor}
      onZoom={vi.fn()}
    />,
  );
  expect(screen.getByText("100.0 / 99.0 km/h")).toBeInTheDocument();
  const initialDraws = canvasContext.mock.calls.length;
  rerender(
    <TelemetryCharts
      comparison={c}
      view="performance"
      cursor={20}
      zoom={[0, 20]}
      onCursor={onCursor}
      onZoom={vi.fn()}
    />,
  );
  expect(screen.getByText("120.0 / 119.0 km/h")).toBeInTheDocument();
  expect(screen.getAllByText("+0.200 s")[0]).toBeInTheDocument();
  expect(canvasContext.mock.calls.length).toBe(initialDraws);
  expect(fetcher).not.toHaveBeenCalled();
});
