import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "@/lib/api/client";
import type { DriverState, StrategyComparisonResponse } from "@/lib/api/types";

import { StrategyPanel } from "./StrategyPanel";

const driver: DriverState = {
  driver_id: "driver-81",
  racing_number: 81,
  abbreviation: "PIA",
  full_name: "Oscar Piastri",
  team_name: "McLaren",
  status: "active",
  position: 1,
  laps_completed: 30,
  current_stint: 2,
  compound: "hard",
  tyre_age_laps: 13,
  last_lap_time_ms: 81_000,
  in_pit: false,
  pit_stop_count: 1,
  last_pit_lap: 17,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("StrategyPanel", () => {
  it("rejects a new-tyre lap that has already started in the driver's own lap count", () => {
    render(
      <StrategyPanel
        sessionId="session"
        driver={driver}
        cursorMs={5_840_332}
        referenceLap={30}
      />,
    );
    fireEvent.change(screen.getByLabelText("First lap on new tyres"), {
      target: { value: "31" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare strategy" }));
    expect(screen.getByText(/driver-own lap 32 or later/)).toBeInTheDocument();
  });

  it("submits paired candidates and renders a fail-closed response", async () => {
    const compare = vi.spyOn(api, "compareStrategies").mockResolvedValue({
      status: "unavailable",
      availability_reason: "neutralized_or_unknown_track",
      session_id: "session",
      driver_id: driver.driver_id,
      cursor: { time_ms: 5_840_332, lap: null },
      model_version: "1.0.0",
      dataset_digest: "digest",
      simulation_version: "1.0.0",
      assumptions: [],
      outcome: null,
    });
    render(
      <StrategyPanel
        sessionId="session"
        driver={driver}
        cursorMs={5_840_332}
        referenceLap={30}
      />,
    );
    fireEvent.change(screen.getByLabelText("Race laps override"), {
      target: { value: "70" },
    });
    fireEvent.change(screen.getByLabelText("First lap on new tyres"), {
      target: { value: "45" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare strategy" }));
    await waitFor(() => expect(compare).toHaveBeenCalledOnce());
    expect(
      await screen.findByText("neutralized or unknown track"),
    ).toBeInTheDocument();
    const request = compare.mock.calls[0]?.[1];
    expect(request?.strategies).toHaveLength(2);
    expect(request?.scenario.scheduled_total_laps).toBe(70);
  });

  it("uses canonical published distance when the override is blank", async () => {
    const compare = vi.spyOn(api, "compareStrategies").mockResolvedValue({
      status: "unavailable",
      availability_reason: "scheduled_distance_required",
      session_id: "session",
      driver_id: driver.driver_id,
      cursor: { time_ms: 5_840_332, lap: null },
      model_version: "1.0.0",
      dataset_digest: "digest",
      simulation_version: "1.1.0",
      assumptions: [],
      outcome: null,
    });
    render(
      <StrategyPanel
        sessionId="session"
        driver={driver}
        cursorMs={5_840_332}
        referenceLap={30}
      />,
    );
    fireEvent.change(screen.getByLabelText("First lap on new tyres"), {
      target: { value: "45" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare strategy" }));
    await waitFor(() => expect(compare).toHaveBeenCalledOnce());
    expect(
      compare.mock.calls[0]?.[1].scenario.scheduled_total_laps,
    ).toBeUndefined();
  });

  it("aborts and hides an obsolete result when strategy inputs change", async () => {
    let resolveFirst: ((value: StrategyComparisonResponse) => void) | undefined;
    const compare = vi
      .spyOn(api, "compareStrategies")
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValueOnce({
        status: "unavailable",
        availability_reason: "new_context",
        session_id: "session",
        driver_id: driver.driver_id,
        cursor: { time_ms: 5_840_332, lap: null },
        model_version: "1.0.0",
        dataset_digest: "digest",
        simulation_version: "1.1.0",
        assumptions: [],
        outcome: null,
      });
    render(
      <StrategyPanel
        sessionId="session"
        driver={driver}
        cursorMs={5_840_332}
        referenceLap={30}
      />,
    );
    fireEvent.change(screen.getByLabelText("First lap on new tyres"), {
      target: { value: "45" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare strategy" }));
    await waitFor(() => expect(compare).toHaveBeenCalledOnce());
    const firstSignal = compare.mock.calls[0]?.[2]?.signal;

    fireEvent.change(screen.getByLabelText("Fit compound"), {
      target: { value: "soft" },
    });
    await waitFor(() => expect(firstSignal?.aborted).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Compare strategy" }));
    expect(await screen.findByText("new context")).toBeInTheDocument();

    resolveFirst?.({
      status: "unavailable",
      availability_reason: "obsolete_context",
      session_id: "session",
      driver_id: driver.driver_id,
      cursor: { time_ms: 5_840_332, lap: null },
      model_version: "1.0.0",
      dataset_digest: "digest",
      simulation_version: "1.1.0",
      assumptions: [],
      outcome: null,
    });
    await waitFor(() =>
      expect(screen.queryByText("obsolete context")).not.toBeInTheDocument(),
    );
  });
});
