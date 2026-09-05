import {
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

function installCanonicalFetch(failFirstState = false) {
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
          ],
          total: 1,
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
          ],
          offset: 0,
          limit: 1_000,
          total: 1,
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
        return response(canonicalState());
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
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
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
});
