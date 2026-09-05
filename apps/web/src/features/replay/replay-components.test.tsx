import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  DriverState,
  TimelineEvent,
  TrackPosition,
} from "@/lib/api/types";

import { EventTimeline } from "./EventTimeline";
import { DriverInspector } from "./DriverInspector";
import { ReplayController } from "./ReplayController";
import { WeatherPanel } from "./SignalPanels";
import { TimingTower } from "./TimingTower";
import { TrackMap } from "./TrackMap";
import type { ReplayControllerState } from "./useReplayController";

const driver: DriverState = {
  driver_id: "driver-44",
  racing_number: 44,
  abbreviation: "HAM",
  full_name: "Lewis Hamilton",
  team_name: "Mercedes",
  status: "active",
  position: 2,
  laps_completed: 10,
  current_stint: 1,
  compound: "medium",
  tyre_age_laps: 10,
  last_lap_time_ms: 91_000,
  in_pit: false,
  pit_stop_count: 0,
  last_pit_lap: null,
};
const point = (time: number, x: number, y: number): TrackPosition => ({
  driver_id: driver.driver_id,
  session_time_ms: time,
  x_m: x,
  y_m: y,
  z_m: null,
  raw_status: null,
});
const event: TimelineEvent = {
  event_id: "event-1",
  session_time_ms: 5_000,
  priority: 50,
  sequence: 1,
  event_type: "driver-pit-entered",
  driver_id: driver.driver_id,
  source: "canonical",
  source_key: null,
  payload: { lap_number: 10 },
};

describe("replay panels", () => {
  afterEach(cleanup);
  it("selects a factual timing row without inventing gaps", () => {
    const onSelect = vi.fn();
    render(
      <TimingTower
        drivers={[driver]}
        selectedDriverId={null}
        onSelect={onSelect}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /ham, mercedes/i }));
    expect(onSelect).toHaveBeenCalledWith("driver-44");
    expect(screen.getByLabelText("Gap unavailable")).toHaveTextContent("—");
  });

  it("exposes terminal driver status and the complete timing-row meaning", () => {
    render(
      <TimingTower
        drivers={[{ ...driver, status: "retired" }]}
        selectedDriverId={null}
        onSelect={vi.fn()}
      />,
    );
    const row = screen.getByRole("button", { name: /status retired/i });
    expect(row).toHaveClass("timing-row--terminal");
    expect(row).toHaveAccessibleName(/medium tyre, 10 laps old/i);
    expect(within(row).getByText("RETIRED")).toBeInTheDocument();
  });

  it("shows the selected driver's canonical lap count and last pit lap", () => {
    render(
      <DriverInspector
        driver={{ ...driver, last_pit_lap: 8, pit_stop_count: 1 }}
        laps={[]}
      />,
    );
    expect(
      within(screen.getByText("Laps completed").parentElement!).getByText("10"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByText("Last pit lap").parentElement!).getByText("8"),
    ).toBeInTheDocument();
  });

  it("renders canonical track coordinates and labels current cars", () => {
    render(
      <TrackMap
        trace={[point(0, 0, 0), point(1, 100, 50)]}
        positions={[point(9_000, 40, 20)]}
        cursorMs={10_000}
        drivers={[driver]}
        selectedDriverId={driver.driver_id}
        loading={false}
      />,
    );
    expect(
      screen.getByRole("img", { name: /1 current driver position/i }),
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("img")).getByText("HAM"),
    ).toBeInTheDocument();
  });

  it("renders a structured unavailable state without track coordinates", () => {
    render(
      <TrackMap
        trace={[]}
        positions={[]}
        cursorMs={10_000}
        drivers={[driver]}
        selectedDriverId={null}
        loading={false}
        availability="unsupported"
      />,
    );
    expect(screen.getByText("Track position data unavailable")).toBeVisible();
    expect(
      screen.getByText(/canonical availability: unsupported/i),
    ).toBeVisible();
  });

  it("seeks the global cursor from a significant timeline marker", () => {
    const onSeek = vi.fn();
    render(
      <EventTimeline
        events={[event]}
        minimumMs={0}
        maximumMs={10_000}
        cursorMs={1_000}
        onSeek={onSeek}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /seek to 00:00:05/i }));
    expect(onSeek).toHaveBeenCalledWith(5_000);
  });

  it("renders canonical P1-reference lap ticks", () => {
    const { container } = render(
      <EventTimeline
        events={[
          {
            ...event,
            event_id: "leader-lap-1",
            event_type: "driver-position-changed",
            session_time_ms: 4_000,
            payload: { lap_number: 1, position: 1 },
          },
        ]}
        minimumMs={0}
        maximumMs={10_000}
        cursorMs={1_000}
        onSeek={vi.fn()}
      />,
    );
    expect(container.querySelector(".lap-marker")).toHaveAttribute(
      "title",
      "Reference lap 1 · 00:00:04",
    );
  });

  it("places adjacent event bins far enough apart for distinct pointer targets", () => {
    render(
      <EventTimeline
        events={[
          { ...event, event_id: "event-a", session_time_ms: 1_000 },
          { ...event, event_id: "event-b", session_time_ms: 2_000 },
        ]}
        minimumMs={0}
        maximumMs={100_000}
        cursorMs={0}
        onSeek={vi.fn()}
      />,
    );
    const markers = screen.getAllByRole("button", { name: /seek to/i });
    expect(markers[0]).toHaveStyle("--event-left: 0%");
    expect(markers[1]).toHaveStyle("--event-left: 2.5%");
  });

  it("reconciles keyboard range changes while throttling pointer scrubs", () => {
    const controller: ReplayControllerState = {
      cursorMs: 500,
      authoritativeCursorMs: 500,
      isPlaying: false,
      speed: 1,
      seek: vi.fn(),
      seekBy: vi.fn(),
      setPlaying: vi.fn(),
      togglePlaying: vi.fn(),
      setSpeed: vi.fn(),
      beginScrub: vi.fn(),
      reconcile: vi.fn(),
    };
    const onLapSeek = vi.fn();
    render(
      <ReplayController
        controller={controller}
        minimumMs={0}
        maximumMs={1_000}
        referenceLap={1}
        maximumLap={2}
        trackStatus="clear"
        onLapSeek={onLapSeek}
        isStateLoading={false}
      />,
    );
    const range = screen.getByRole("slider", { name: "Replay time" });
    fireEvent.change(range, { target: { value: "600" } });
    expect(controller.seek).toHaveBeenLastCalledWith(600, true);
    fireEvent.pointerDown(range);
    fireEvent.change(range, { target: { value: "700" } });
    expect(controller.seek).toHaveBeenLastCalledWith(700, false);
    fireEvent.pointerUp(range);
    expect(controller.reconcile).toHaveBeenCalled();
    expect(
      screen.getByText("Track clear", { selector: ".track-status" }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Previous leader lap" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Next leader lap" }));
    expect(onLapSeek).toHaveBeenCalledWith(2);
  });

  it("distinguishes unavailable weather from a future observation", () => {
    render(<WeatherPanel weather={null} availability="empty" />);
    expect(screen.getByText(/weather data unavailable.*empty/i)).toBeVisible();
  });
});
