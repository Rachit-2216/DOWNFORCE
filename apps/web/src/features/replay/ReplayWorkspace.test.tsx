import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ReplayWorkspace } from "./ReplayWorkspace";

const sessionId = "session-test";
const driverId = "driver-44";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function canonicalState() {
  return {
    replay_version: "1.0.0",
    session_id: sessionId,
    session_time_ms: 1_000,
    reference_lap: 1,
    track_status: "clear",
    weather: null,
    drivers: [
      {
        driver_id: driverId,
        racing_number: 44,
        abbreviation: "HAM",
        full_name: "Lewis Hamilton",
        team_name: "Mercedes",
        status: "active",
        position: 1,
        laps_completed: 1,
        current_stint: 1,
        compound: "medium",
        tyre_age_laps: 1,
        last_lap_time_ms: 91_000,
        in_pit: false,
        pit_stop_count: 0,
        last_pit_lap: null,
      },
    ],
    recent_race_control: [],
    completeness: {
      weather: "empty",
      "race-control": "empty",
      "track-positions": "unsupported",
    },
    data_quality: "partial",
  };
}

function installCanonicalFetch(failFirstState = false, extended = false) {
  let stateRequests = 0;
  const fetcher = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/intelligence?"))
        return response({
          availability: "unavailable",
          reason: "session_finished",
          model_version: "1.0.0",
          dataset_digest: "dataset-digest",
          assumptions: [],
          as_of: {
            time_ms: Number(new URL(url).searchParams.get("time_ms")),
            lap: null,
          },
          pace: null,
          tyre_degradation: null,
          pit_loss: null,
        });
      if (url.includes("/drivers"))
        return response({
          items: [
            {
              driver_id: driverId,
              racing_number: 44,
              abbreviation: "HAM",
              full_name: "Lewis Hamilton",
              team_name: "Mercedes",
              country_code: "GBR",
            },
            ...(extended
              ? [
                  {
                    driver_id: "driver-1",
                    racing_number: 1,
                    abbreviation: "VER",
                    full_name: "Max Verstappen",
                    team_name: "Red Bull",
                    country_code: "NLD",
                  },
                ]
              : []),
          ],
          total: extended ? 2 : 1,
        });
      if (url.includes("/laps?"))
        return response({
          items: [
            {
              driver_id: driverId,
              lap_number: 1,
              lap_start_time_ms: 0,
              lap_end_time_ms: 1_000,
              lap_time_ms: 1_000,
              sector_1_time_ms: null,
              sector_2_time_ms: null,
              sector_3_time_ms: null,
              stint_number: 1,
              compound: "medium",
              raw_compound: null,
              tyre_life_laps: 1,
              is_personal_best: true,
              is_accurate: true,
              is_generated: false,
              is_deleted: false,
              deleted_reason: null,
              raw_track_status: null,
            },
            ...(extended
              ? [
                  {
                    driver_id: driverId,
                    lap_number: 5,
                    lap_start_time_ms: 4000,
                    lap_end_time_ms: 5000,
                    lap_time_ms: 1000,
                  },
                  {
                    driver_id: "driver-1",
                    lap_number: 5,
                    lap_start_time_ms: 4800,
                    lap_end_time_ms: 6000,
                    lap_time_ms: 1200,
                  },
                ]
              : []),
          ],
          offset: 0,
          limit: 1_000,
          total: extended ? 3 : 1,
        });
      if (url.includes("/timeline?"))
        return response({
          items: [
            {
              event_id: "leader-lap-1",
              session_time_ms: 1_000,
              priority: 50,
              sequence: 1,
              event_type: "driver-position-changed",
              driver_id: driverId,
              source: "canonical",
              source_key: null,
              payload: { position: 1, lap_number: 1 },
            },
          ],
          offset: 0,
          limit: 1_000,
          total: 1,
        });
      if (url.includes("/state?")) {
        stateRequests += 1;
        if (failFirstState && stateRequests === 1)
          return response(
            { error: { message: "temporary state failure" } },
            500,
          );
        const state = canonicalState();
        const rawTime = new URL(url).searchParams.get("time_ms");
        state.session_time_ms = rawTime === null ? 1000 : Number(rawTime);
        if (extended)
          state.drivers.push({
            ...state.drivers[0],
            driver_id: "driver-1",
            racing_number: 1,
            abbreviation: "VER",
            full_name: "Max Verstappen",
            team_name: "Red Bull",
            position: 2,
          });
        return response(state);
      }
      if (url.endsWith(`/api/v1/sessions/${sessionId}`))
        return response({
          session_id: sessionId,
          dataset_id: "dataset-test",
          snapshot_id: "snapshot-test",
          session: {
            event_name: "Test Grand Prix",
            season: 2024,
            session_name: "Race",
            circuit_name: "Test Circuit",
            data_quality: "partial",
          },
          provider: {},
          capabilities: {},
          completeness: {},
          tables: {
            events: {
              availability: "available",
              materialized: true,
              row_count: 1,
              min_session_time_ms: 0,
              max_session_time_ms: 10_000,
            },
            laps: {
              availability: "available",
              materialized: true,
              row_count: 1,
              min_session_time_ms: 0,
              max_session_time_ms: 1_000,
            },
            track_positions: {
              availability: "unsupported",
              materialized: false,
              row_count: 0,
              min_session_time_ms: null,
              max_session_time_ms: null,
            },
            weather: {
              availability: "empty",
              materialized: true,
              row_count: 0,
              min_session_time_ms: null,
              max_session_time_ms: null,
            },
            race_control: {
              availability: "empty",
              materialized: true,
              row_count: 0,
              min_session_time_ms: null,
              max_session_time_ms: null,
            },
          },
          warnings: [],
          canonical_schema_version: "1.0.0",
          normalization_version: "1.0.0",
          timeline_version: "1.0.0",
          replay_version: "1.0.0",
        });
      throw new Error(`Unexpected request: ${url}`);
    });
  return { fetcher, stateRequests: () => stateRequests };
}

describe("replay workspace integration", () => {
  it("does not let a late lap lookup override a newer manual time seek", async () => {
    const { fetcher } = installCanonicalFetch(false, true);
    const original = fetcher.getMockImplementation()!;
    let finish!: (value: Response) => void;
    fetcher.mockImplementation((input, init) =>
      String(input).includes("/state?lap=")
        ? new Promise((resolve) => {
            finish = resolve;
          })
        : original(input, init),
    );
    render(<ReplayWorkspace sessionId={sessionId} />);
    await screen.findByLabelText("Track status clear");
    fireEvent.click(screen.getByRole("button", { name: "Next leader lap" }));
    fireEvent.change(screen.getByRole("slider", { name: "Replay time" }), {
      target: { value: "500" },
    });
    await act(async () =>
      finish(response({ ...canonicalState(), session_time_ms: 5000 })),
    );
    expect(new URLSearchParams(window.location.search).get("t")).toBe("500");
  });
  it("does not switch the default selected driver when the race leader changes", async () => {
    window.history.replaceState(null, "", "/?t=1000");
    const { fetcher } = installCanonicalFetch(false, true);
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (input, init) => {
      const result = await original(input, init);
      if (!String(input).includes("/state?time_ms=5000")) return result;
      const body = await result.json();
      body.drivers[0].position = 2;
      body.drivers.find(
        (driver: { driver_id: string; position: number }) =>
          driver.driver_id === "driver-1",
      ).position = 1;
      return response(body);
    });
    render(<ReplayWorkspace sessionId={sessionId} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Selected driver")).toHaveValue(driverId),
    );
    window.history.replaceState(null, "", "/?t=5000");
    fireEvent(window, new PopStateEvent("popstate"));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("t")).toBe("5000"),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Selected driver")).toHaveValue(driverId),
    );
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });
  it("keeps product navigation present during initial loading", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () => new Promise(() => {}),
    );
    render(<ReplayWorkspace sessionId={sessionId} />);
    expect(
      screen.getByRole("navigation", { name: "Product navigation" }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: /Choose another race/i }),
    ).toHaveAttribute("href", "/app/replays");
  });
  it("does not block usable race timing on an optional circuit trace", async () => {
    const { fetcher } = installCanonicalFetch();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation(async (input, init) => {
      if (String(input).includes("/track-positions"))
        return new Promise<Response>(() => {});
      const result = await original(input, init);
      if (String(input).endsWith(`/api/v1/sessions/${sessionId}`)) {
        const body = await result.json();
        body.tables.track_positions.availability = "available";
        return response(body);
      }
      return result;
    });
    render(<ReplayWorkspace sessionId={sessionId} />);
    expect(
      await screen.findByRole("button", { name: /play replay/i }),
    ).toBeVisible();
    expect(
      await screen.findByRole("button", { name: /status active/i }),
    ).toBeVisible();
  });

  it("does not show later timing data during an unresolved backwards seek", async () => {
    window.history.replaceState(null, "", "/?t=5000&mode=drivers");
    const { fetcher } = installCanonicalFetch();
    const original = fetcher.getMockImplementation()!;
    fetcher.mockImplementation((input, init) =>
      String(input).includes("/state?time_ms=500&") ||
      String(input).endsWith("/state?time_ms=500")
        ? new Promise<Response>(() => {})
        : original(input, init),
    );
    render(<ReplayWorkspace sessionId={sessionId} />);
    await screen.findByRole("button", { name: /status active/i });
    window.history.replaceState(null, "", "/?t=500&mode=drivers");
    fireEvent(window, new PopStateEvent("popstate"));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("t")).toBe("500"),
    );
    expect(
      screen.queryByRole("button", { name: /status active/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/loading race state/i)).toBeVisible();
  });

  it("shares driver and bounded completed-lap context across workspace modes", async () => {
    window.history.replaceState(null, "", "/?mode=intelligence");
    const { fetcher } = installCanonicalFetch(false, true);
    render(<ReplayWorkspace sessionId={sessionId} />);
    const driver = await screen.findByLabelText("Selected driver");
    await waitFor(() => expect(driver).toHaveValue(driverId));
    fireEvent.click(
      screen.getByRole("button", { name: "Jump to completed lap 5" }),
    );
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("t")).toBe("5000"),
    );
    fireEvent.change(driver, { target: { value: "driver-1" } });
    fireEvent.click(screen.getByRole("button", { name: /^Strategy$/ }));
    expect(screen.getByLabelText("Selected driver")).toHaveValue("driver-1");
    expect(new URLSearchParams(window.location.search).get("t")).toBe("5000");
    fireEvent.click(
      screen.getByRole("button", { name: "Jump to completed lap 5" }),
    );
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("t")).toBe("6000"),
    );
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("/state?time_ms=6000"),
      ),
    ).toBe(true);
    window.history.replaceState(
      null,
      "",
      "/?mode=intelligence&driver=44&t=3000",
    );
    fireEvent(window, new PopStateEvent("popstate"));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("t")).toBe("3000"),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Selected driver")).toHaveValue(driverId),
    );
    await waitFor(() =>
      expect(
        fetcher.mock.calls.some(([url]) =>
          String(url).includes("/intelligence?time_ms=3000"),
        ),
      ).toBe(true),
    );
  });

  it("keeps replay usable when canonical track, weather and control data are unavailable", async () => {
    window.history.replaceState(null, "", "/?mode=replay&view=conditions");
    const { fetcher } = installCanonicalFetch();
    render(<ReplayWorkspace sessionId={sessionId} />);

    expect(
      await screen.findByText(/weather data unavailable.*empty/i),
    ).toBeVisible();
    expect(
      screen.getByText(/race-control data unavailable.*empty/i),
    ).toBeVisible();
    expect(
      await screen.findByRole("button", { name: /play replay/i }),
    ).toBeVisible();
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("/track-positions"),
      ),
    ).toBe(false);
    expect(
      screen.getByRole("link", { name: "Skip to main content" }),
    ).toHaveAttribute("href", "#main-content");
    expect(document.getElementById("main-content")).toBeInTheDocument();
  });

  it("keeps bypass navigation available when replay loading fails", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));

    render(<ReplayWorkspace sessionId={sessionId} />);

    expect(
      await screen.findByRole("heading", { name: "Replay unavailable" }),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Skip to main content" }),
    ).toHaveAttribute("href", "#main-content");
    expect(screen.getByRole("alert")).toHaveAttribute("id", "main-content");
  });

  it("performs a new authoritative request when state retry is selected", async () => {
    const { stateRequests } = installCanonicalFetch(true);
    render(<ReplayWorkspace sessionId={sessionId} />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(stateRequests()).toBe(2));
    expect(
      await screen.findByRole("button", { name: /status active/i }),
    ).toBeVisible();
    expect(
      screen.queryByText("State reconciliation failed."),
    ).not.toBeInTheDocument();
  });

  it("requests intelligence at the actual replay cursor", async () => {
    window.history.replaceState(null, "", "/?t=5000&mode=intelligence");
    const { fetcher } = installCanonicalFetch();

    render(<ReplayWorkspace sessionId={sessionId} />);

    expect(
      await screen.findByText("session finished", { exact: false }),
    ).toBeVisible();
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("/intelligence?time_ms=5000"),
      ),
    ).toBe(true);
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("/intelligence?time_ms=1000"),
      ),
    ).toBe(false);
  });

  it.each(["local", "visitor"])(
    "uses %s replay telemetry availability",
    async (profile) => {
      vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", profile);
      installCanonicalFetch();
      render(<ReplayWorkspace sessionId={sessionId} />);
      await screen.findByRole("button", { name: /status active/i });
      if (profile === "visitor") {
        expect(
          screen.queryByRole("link", { name: "Telemetry Lab" }),
        ).not.toBeInTheDocument();
        vi.unstubAllEnvs();
        return;
      }
      const link = await screen.findByRole("link", { name: "Telemetry Lab" });
      await waitFor(() =>
        expect(link.getAttribute("href")).toContain("driver_a=44"),
      );
      expect(link.getAttribute("href")).toContain(
        "replay_session_id=session-test",
      );
      expect(
        new URL(
          link.getAttribute("href")!,
          window.location.origin,
        ).searchParams.has("session"),
      ).toBe(false);
      expect(link.getAttribute("href")).toContain("lap_a=1");
      vi.unstubAllEnvs();
    },
  );
});
