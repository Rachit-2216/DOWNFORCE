import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as client from "@/lib/api/client";
import type {
  EntityAnalyticsResponse,
  RaceAnalyticsResponse,
} from "@/lib/api/types";
import { RaceAnalytics } from "./RaceAnalytics";
import { EntityProfile } from "./EntityProfile";
import { EntityDirectory } from "./EntityDirectory";
import { CompareAnalytics } from "./CompareAnalytics";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
const coverage = {
  sample_count: 1,
  race_count: 1,
  eligible_race_count: 1,
  missing_count: 0,
  verified_count: 1,
  good_count: 0,
  quality_exclusions: 0,
  ratio: 1,
  analytics_version: "test",
  archive_source_revision: "test",
};
const summary = {
  starts: 0,
  finishes: 0,
  wins: 0,
  podiums: 0,
  points: 0,
  dnf: 0,
  dns: 0,
  dsq: 0,
  average_grid: null,
  average_grid_samples: 0,
  average_finish: null,
  average_finish_samples: 0,
  positions_gained: 0,
  positions_gained_samples: 0,
  pit_stops: 0,
  pit_coverage_races: 0,
};
function race(name: string): RaceAnalyticsResponse {
  return {
    analytics_version: "test",
    archive_source_revision: "test",
    event: { event_id: "event-one", event_name: name },
    summary: {},
    drivers: [],
    biggest_movers: [],
    position_progression: [],
    coverage: { results: coverage, laps: coverage, pits: coverage },
  };
}
function profile(): EntityAnalyticsResponse {
  return {
    analytics_version: "test",
    archive_source_revision: "test",
    entity: { driver_name: "Test Driver" },
    summary: {},
    races: {
      items: [{ session_id: "race-one", season: 2024, event_name: "Race One" }],
      total: 2,
      offset: 0,
      limit: 1,
    },
    coverage: { results: coverage },
  };
}
it("retries a failed race without leaving the analytics workspace", async () => {
  vi.spyOn(client, "getRaceAnalytics")
    .mockRejectedValueOnce(new Error("Temporary failure"))
    .mockResolvedValueOnce(race("Monaco"));
  render(<RaceAnalytics sessionId="race" />);
  await screen.findByText("Temporary failure");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(
    await screen.findByRole("heading", { name: "Monaco" }),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open replay" })).toHaveAttribute(
    "href",
    "/app/events/event-one/workspace?mode=replay",
  );
});
it("ignores an old race response after navigation even if transport ignores abort", async () => {
  let resolveOld!: (data: RaceAnalyticsResponse) => void;
  vi.spyOn(client, "getRaceAnalytics").mockImplementation((id) =>
    id === "old"
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : Promise.resolve(race("New race")),
  );
  const view = render(<RaceAnalytics sessionId="old" />);
  view.rerender(<RaceAnalytics sessionId="new" />);
  await screen.findByRole("heading", { name: "New race" });
  await act(async () => resolveOld(race("Old race")));
  expect(
    screen.queryByRole("heading", { name: "Old race" }),
  ).not.toBeInTheDocument();
});
it("retains an entity profile and its ledger when loading more fails", async () => {
  vi.spyOn(client, "getEntityAnalytics")
    .mockResolvedValueOnce(profile())
    .mockRejectedValueOnce(new Error("More history failed"));
  render(<EntityProfile kind="drivers" id="test" />);
  await screen.findByRole("heading", { name: "Test Driver" });
  fireEvent.click(screen.getByRole("button", { name: /Load.*races/i }));
  await screen.findByText("More history failed");
  expect(
    screen.getByRole("heading", { name: "Test Driver" }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("link", { name: /2024.*Race One/ }),
  ).toBeInTheDocument();
});
it("offers recovery when the comparison directory fails before a pair can be selected", async () => {
  vi.spyOn(client, "getAnalyticsEntities").mockRejectedValue(
    new Error("Directory offline"),
  );
  render(<CompareAnalytics />);
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  await act(async () => {});
  expect(client.getAnalyticsEntities).toHaveBeenCalledTimes(2);
});
it("shows an explicit loading state while comparison choices are pending", () => {
  vi.spyOn(client, "getAnalyticsEntities").mockImplementation(
    () => new Promise(() => {}),
  );
  render(<CompareAnalytics />);
  expect(screen.getByRole("status")).toHaveTextContent(
    "Loading comparison choices",
  );
});
it("does not append an old directory page after changing entity kind", async () => {
  let resolvePage!: (
    data: Awaited<ReturnType<typeof client.getAnalyticsEntities>>,
  ) => void;
  vi.spyOn(client, "getAnalyticsEntities").mockImplementation(
    (kind, options) =>
      options?.offset
        ? new Promise((resolve) => {
            resolvePage = resolve;
          })
        : Promise.resolve({
            items: [
              {
                ...summary,
                entity_id: kind,
                entity_name:
                  kind === "drivers" ? "Driver record" : "Team record",
                start_season: 2020,
                end_season: 2024,
              },
            ],
            total: 2,
            offset: 0,
            limit: 1,
            coverage,
            analytics_version: "test",
            archive_source_revision: "test",
          }),
  );
  const view = render(<EntityDirectory kind="drivers" />);
  await screen.findByRole("link", { name: /Driver record/ });
  fireEvent.click(screen.getByRole("button", { name: /Load next drivers/ }));
  view.rerender(<EntityDirectory kind="constructors" />);
  await screen.findByRole("link", { name: /Team record/ });
  await act(async () =>
    resolvePage({
      items: [
        {
          ...summary,
          entity_id: "old",
          entity_name: "Old driver",
          start_season: 2000,
          end_season: 2024,
        },
      ],
      total: 2,
      offset: 1,
      limit: 1,
      coverage,
      analytics_version: "test",
      archive_source_revision: "test",
    }),
  );
  expect(
    screen.queryByRole("link", { name: /Old driver/ }),
  ).not.toBeInTheDocument();
});
