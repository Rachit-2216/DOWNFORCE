import { describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  compareAnalytics,
  getAnalyticsRankings,
  getHealth,
  getRaceState,
  getTrackPositions,
  resolveApiBaseUrl,
} from "./client";

describe("API client", () => {
  it("validates and returns the health contract", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          status: "ok",
          service: "downforce-api",
          version: "0.1.0",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(
      getHealth({ baseUrl: "http://api.test", fetcher }),
    ).resolves.toEqual({
      status: "ok",
      service: "downforce-api",
      version: "0.1.0",
    });
    expect(fetcher).toHaveBeenCalledWith(
      "http://api.test/health",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });

  it("rejects a malformed health payload", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "maybe" }), { status: 200 }),
      );

    await expect(
      getHealth({ baseUrl: "http://api.test", fetcher }),
    ).rejects.toThrow("invalid health response");
  });

  it("fails clearly when production configuration is missing", () => {
    expect(() => resolveApiBaseUrl(undefined, "production")).toThrow(
      ApiClientError,
    );
  });

  it("encodes canonical identifiers and mutually exclusive time cursors", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ session_time_ms: 1200 }), {
        status: 200,
      }),
    );
    await getRaceState(
      "session/id",
      { timeMs: 1_200.4 },
      { baseUrl: "http://api.test", fetcher },
    );
    expect(fetcher).toHaveBeenCalledWith(
      "http://api.test/api/v1/sessions/session%2Fid/state?time_ms=1200",
      expect.any(Object),
    );
  });

  it("bounds track-position requests through explicit canonical filters", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ items: [], offset: 0, limit: 5000, total: 0 }),
          { status: 200 },
        ),
      );
    await getTrackPositions(
      "session-test",
      { fromMs: 5_000, toMs: 10_000 },
      { baseUrl: "http://api.test", fetcher },
    );
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "http://api.test/api/v1/sessions/session-test/track-positions?from_ms=5000&to_ms=10000&limit=5000",
    );
  });

  it("persists bounded analytics filters in the ranking request", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      );
    await getAnalyticsRankings(
      {
        entityType: "driver",
        metric: "average_finish",
        minimumStarts: 20,
        startSeason: 2018,
        endSeason: 2024,
        limit: 50,
      },
      { baseUrl: "http://api.test", fetcher },
    );
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      "http://api.test/api/v1/analytics/rankings?start_season=2018&end_season=2024&entity_type=driver&metric=average_finish&minimum_starts=20&limit=50",
    );
  });

  it("sends comparison semantics as a typed JSON body", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(JSON.stringify({ common_race_count: 1 }), { status: 200 }),
      );
    const request = {
      entity_type: "driver" as const,
      entity_a: "hamilton",
      entity_b: "max_verstappen",
      mode: "common_races" as const,
      filters: { start_season: 2021, end_season: 2021 },
    };
    await compareAnalytics(request, { baseUrl: "http://api.test", fetcher });
    expect(fetcher).toHaveBeenCalledWith(
      "http://api.test/api/v1/analytics/compare",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify(request),
      }),
    );
  });
});
