import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { DriverState, IntelligenceResponse } from "@/lib/api/types";

import { IntelligencePanel } from "./IntelligencePanel";

const driver: DriverState = {
  driver_id: "driver-44",
  racing_number: 44,
  abbreviation: "HAM",
  full_name: "Lewis Hamilton",
  team_name: "Mercedes",
  status: "active",
  position: 1,
  laps_completed: 20,
  current_stint: 1,
  compound: "medium",
  tyre_age_laps: 12,
  last_lap_time_ms: 91_000,
  in_pit: false,
  pit_stop_count: 0,
  last_pit_lap: null,
};

const available: IntelligenceResponse = {
  availability: "available",
  reason: null,
  model_version: "1.0.0",
  dataset_digest: "fixture-digest",
  assumptions: ["next eligible lap is dry and green flag"],
  as_of: { time_ms: 1_800_000, lap: 20 },
  pace: {
    label: "Predicted next representative green-flag lap",
    predicted_lap_time_ms: 90_500,
    observed_latest_lap_time_ms: 91_000,
    interval_80: { lower_ms: 89_700, upper_ms: 91_300 },
    interval_90: { lower_ms: 89_200, upper_ms: 91_800 },
  },
  tyre_degradation: {
    label: "Predicted pace residual under held-constant race conditions",
    compound: "medium",
    current_tyre_age_laps: 12,
    predicted_residual_ms: 220,
    interval_80_half_width_ms: 800,
    interval_90_half_width_ms: 1_200,
    curve: [1, 2, 3, 4, 5].map((laps) => ({
      laps_ahead: laps,
      tyre_age_laps: 12 + laps,
      predicted_pace_delta_ms: laps * 100,
    })),
  },
  pit_loss: {
    label: "Estimated dry green-flag effective pit-cycle loss",
    circuit: "Silverstone Circuit",
    estimated_effective_loss_ms: 22_000,
    interval_80: { lower_ms: 19_000, upper_ms: 25_000 },
    interval_90: { lower_ms: 18_000, upper_ms: 26_000 },
    stationary_duration_ms: null,
    stationary_duration_reason: "not_observed_in_canonical_data",
  },
};

describe("IntelligencePanel", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("labels observed and predicted values without strategy language", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(available), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    render(
      <IntelligencePanel
        sessionId="session-1"
        driver={driver}
        cursorMs={1_800_000}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      /reconciling predictions/i,
    );
    await waitFor(() => expect(screen.getByText("1:30.500")).toBeVisible());
    expect(screen.getByText(/observed latest/i)).toBeVisible();
    expect(screen.getByText(/no strategy recommendation/i)).toBeVisible();
    expect(document.body).not.toHaveTextContent(/pit now|should pit/i);
  });

  it("keeps replay usable through a structured unavailable state", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            availability: "unavailable",
            reason: "insufficient_clean_history",
            model_version: "1.0.0",
            dataset_digest: "fixture-digest",
            assumptions: [],
            as_of: { time_ms: 100, lap: null },
            pace: null,
            tyre_degradation: null,
            pit_loss: null,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    render(
      <IntelligencePanel
        sessionId="session-1"
        driver={driver}
        cursorMs={100}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText(/prediction unavailable at this cursor/i),
      ).toBeVisible(),
    );
    expect(screen.getByText(/insufficient clean history/i)).toBeVisible();
    expect(screen.getByText(/replay remains fully available/i)).toBeVisible();
  });
});
