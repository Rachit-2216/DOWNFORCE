import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "@/lib/api/client";
import type {
  AnalyticsListItem,
  AnalyticsPageResponse,
  ComparisonAnalyticsResponse,
  RankingAnalyticsResponse,
} from "@/lib/api/types";

import { CompareAnalytics } from "./CompareAnalytics";
import { RankingsAnalytics } from "./RankingsAnalytics";

const coverage = {
  sample_count: 10,
  race_count: 10,
  eligible_race_count: 10,
  missing_count: 0,
  verified_count: 10,
  good_count: 0,
  quality_exclusions: 0,
  analytics_version: "1.1.0",
  archive_source_revision: "fixture",
  ratio: 1,
};

function entity(entity_id: string, entity_name: string): AnalyticsListItem {
  return {
    entity_id,
    entity_name,
    start_season: 2000,
    end_season: 2026,
    starts: 100,
    finishes: 90,
    wins: 10,
    podiums: 20,
    points: 1_000,
    dnf: 10,
    dns: 0,
    dsq: 0,
    average_grid: 4,
    average_grid_samples: 100,
    average_finish: 5,
    average_finish_samples: 90,
    positions_gained: 20,
    positions_gained_samples: 90,
    pit_stops: 80,
    pit_coverage_races: 70,
  };
}

const entities = [
  entity("hamilton", "Lewis Hamilton"),
  entity("russell", "George Russell"),
  entity("norris", "Lando Norris"),
];

function entityPage(items = entities): AnalyticsPageResponse {
  return {
    offset: 0,
    limit: 100,
    total: items.length,
    items,
    coverage,
    analytics_version: "1.1.0",
    archive_source_revision: "fixture",
  };
}

function rankingPage(
  items: RankingAnalyticsResponse["items"],
  options: { offset?: number; total?: number; start?: number } = {},
): RankingAnalyticsResponse {
  return {
    offset: options.offset ?? 0,
    limit: 100,
    total: options.total ?? items.length,
    entity_type: "driver",
    metric: "average_finish",
    minimum_starts: 20,
    items,
    coverage,
    analytics_version: "1.1.0",
    archive_source_revision: `fixture-${options.start ?? 2000}`,
  };
}

function comparison(
  a: string,
  b: string,
  start: number,
  end: number,
): ComparisonAnalyticsResponse {
  const names = new Map(
    entities.map((item) => [item.entity_id, item.entity_name]),
  );
  return {
    entity_type: "driver",
    mode: "common_races",
    filters: { start_season: start, end_season: end },
    entity_a: { entity_name: names.get(a) ?? a, summary: { wins: 2 } },
    entity_b: { entity_name: names.get(b) ?? b, summary: { wins: 1 } },
    common_race_count: 3,
    head_to_head: {
      denominator: 3,
      a_finished_ahead: 2,
      b_finished_ahead: 1,
      excluded_non_comparable: 0,
      tied: 0,
    },
    coverage,
    analytics_version: "1.1.0",
    archive_source_revision: "fixture",
  };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("analytics URL state", () => {
  it("restores ranking filters from a deep link and follows popstate", async () => {
    const getRankings = vi.spyOn(api, "getAnalyticsRankings").mockResolvedValue(
      rankingPage([
        {
          entity_id: "hamilton",
          entity_name: "Lewis Hamilton",
          rank: 1,
          value: 3.8,
          starts: 392,
          race_count: 392,
          sample_count: 359,
        },
      ]),
    );
    window.history.replaceState(
      null,
      "",
      "/app/analytics/rankings?entity=driver&metric=average_finish&minimum_starts=20&start_season=2000&end_season=2026",
    );

    render(<RankingsAnalytics />);

    await waitFor(() =>
      expect(getRankings).toHaveBeenLastCalledWith(
        expect.objectContaining({
          entityType: "driver",
          metric: "average_finish",
          minimumStarts: 20,
          startSeason: 2000,
          endSeason: 2026,
        }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(screen.getByLabelText("Metric")).toHaveValue("average_finish");
    expect(screen.getByLabelText("Minimum starts")).toHaveValue("20");

    const pushState = vi.spyOn(window.history, "pushState");
    fireEvent.change(screen.getByLabelText("Minimum starts"), {
      target: { value: "10" },
    });
    expect(pushState).toHaveBeenCalledOnce();
    expect(window.location.search).toContain("minimum_starts=10");

    window.history.replaceState(
      null,
      "",
      "/app/analytics/rankings?entity=constructor&metric=points&minimum_starts=5&start_season=2014&end_season=2024",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() => {
      expect(screen.getByLabelText("Entity")).toHaveValue("constructor");
      expect(screen.getByLabelText("Metric")).toHaveValue("points");
      expect(screen.getByLabelText("From")).toHaveValue(2014);
      expect(screen.getByLabelText("To")).toHaveValue(2024);
    });
  });

  it("does not merge a stale ranking page after the year range changes", async () => {
    const firstItems = Array.from({ length: 100 }, (_, index) => ({
      entity_id: `old-${index}`,
      entity_name: `Old range ${index}`,
      rank: index + 1,
      value: index + 1,
      starts: 20,
      race_count: 20,
      sample_count: 20,
    }));
    let resolveMore!: (response: RankingAnalyticsResponse) => void;
    const oldMore = new Promise<RankingAnalyticsResponse>((resolve) => {
      resolveMore = resolve;
    });
    vi.spyOn(api, "getAnalyticsRankings").mockImplementation((params) => {
      if (!params) throw new Error("ranking parameters are required");
      if (params.offset) return oldMore;
      if (params.startSeason === 2010)
        return Promise.resolve(
          rankingPage(
            [
              {
                entity_id: "new-range",
                entity_name: "New range only",
                rank: 1,
                value: 1,
                starts: 20,
                race_count: 20,
                sample_count: 20,
              },
            ],
            { start: 2010 },
          ),
        );
      return Promise.resolve(rankingPage(firstItems, { total: 101 }));
    });
    window.history.replaceState(
      null,
      "",
      "/app/analytics/rankings?entity=driver&metric=average_finish&minimum_starts=20&start_season=2000&end_season=2026",
    );
    render(<RankingsAnalytics />);

    fireEvent.click(
      await screen.findByRole("button", { name: /Load next rankings/ }),
    );
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "2010" },
    });
    expect(await screen.findByText("New range only")).toBeVisible();

    await act(async () => {
      resolveMore(
        rankingPage(
          [
            {
              entity_id: "stale-page",
              entity_name: "Stale page entry",
              rank: 101,
              value: 101,
              starts: 20,
              race_count: 20,
              sample_count: 20,
            },
          ],
          { offset: 100, total: 101 },
        ),
      );
      await oldMore;
    });

    expect(screen.getByText("New range only")).toBeVisible();
    expect(screen.queryByText("Stale page entry")).not.toBeInTheDocument();
  });

  it("auto-runs a comparison deep link and restores it on popstate", async () => {
    vi.spyOn(api, "getAnalyticsEntities").mockResolvedValue(entityPage());
    const compare = vi
      .spyOn(api, "compareAnalytics")
      .mockImplementation((request) =>
        Promise.resolve(
          comparison(
            request.entity_a,
            request.entity_b,
            request.filters.start_season,
            request.filters.end_season,
          ),
        ),
      );
    window.history.replaceState(
      null,
      "",
      "/app/analytics/compare?entity=driver&a=hamilton&b=russell&start_season=2022&end_season=2024",
    );

    render(<CompareAnalytics />);

    await waitFor(() =>
      expect(compare).toHaveBeenLastCalledWith(
        expect.objectContaining({
          entity_type: "driver",
          entity_a: "hamilton",
          entity_b: "russell",
          filters: { start_season: 2022, end_season: 2024 },
        }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(await screen.findByText(/finished ahead in 2 of 3/)).toBeVisible();

    const pushState = vi.spyOn(window.history, "pushState");
    fireEvent.change(screen.getByLabelText("B"), {
      target: { value: "norris" },
    });
    expect(pushState).toHaveBeenCalledOnce();
    expect(window.location.search).toContain("b=norris");

    window.history.replaceState(
      null,
      "",
      "/app/analytics/compare?entity=driver&a=russell&b=norris&start_season=2021&end_season=2023",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));

    await waitFor(() =>
      expect(compare).toHaveBeenLastCalledWith(
        expect.objectContaining({
          entity_a: "russell",
          entity_b: "norris",
          filters: { start_season: 2021, end_season: 2023 },
        }),
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
    expect(screen.getByLabelText("A")).toHaveValue("russell");
    expect(screen.getByLabelText("B")).toHaveValue("norris");
  });
});
