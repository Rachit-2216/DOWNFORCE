import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiClientError,
  compareAnalytics,
  getAnalyticsRankings,
  getCatalogSeasons,
  getHealth,
  getRaceState,
  getTrackPositions,
  resolveApiBaseUrl,
} from "./client";

describe("API client", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.useRealTimers();
  });
  it("bounds a hung request and aborts its transport", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn<typeof fetch>(() => new Promise(() => {}));
    const pending = getCatalogSeasons({ fetcher, timeoutMs: 25 });
    const rejected = expect(pending).rejects.toThrow(/took too long/i);
    await vi.advanceTimersByTimeAsync(26);
    await rejected;
    expect(fetcher.mock.calls[0]?.[1]?.signal?.aborted).toBe(true);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("bounds response decoding as well as receiving response headers", async () => {
    vi.useFakeTimers();
    const body = new Response("{}");
    vi.spyOn(body, "json").mockImplementation(() => new Promise(() => {}));
    const pending = getCatalogSeasons({
      fetcher: vi.fn<typeof fetch>().mockResolvedValue(body),
      timeoutMs: 25,
    });
    const rejected = expect(pending).rejects.toThrow(/took too long/i);
    await vi.advanceTimersByTimeAsync(26);
    await rejected;
  });
  it("keeps caller cancellation distinct from a timeout even if fetch ignores abort", async () => {
    const controller = new AbortController();
    const pending = getCatalogSeasons({
      fetcher: vi.fn<typeof fetch>(() => new Promise(() => {})),
      signal: controller.signal,
    });
    const rejected = expect(pending).rejects.toMatchObject({
      name: "AbortError",
    });
    controller.abort();
    await rejected;
  });
  it.each([403, 404, 422, 503])(
    "preserves HTTP %s without silently retrying",
    async (status) => {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Request unavailable" }), {
          status,
        }),
      );
      await expect(getCatalogSeasons({ fetcher })).rejects.toMatchObject({
        status,
      });
      expect(fetcher).toHaveBeenCalledTimes(1);
    },
  );
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

  it("uses the deployed same-origin API without a build-time public env var", async () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    expect(resolveApiBaseUrl()).toBe("");
    expect(resolveApiBaseUrl("/", "production")).toBe("");
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [],
          total: 0,
          event_count: 0,
          completed_event_count: 0,
          latest_completed_event_date: null,
        }),
      ),
    );
    await getCatalogSeasons({ fetcher });
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/v1/catalog/seasons");
  });

  it("keeps local development and explicit API origins supported", () => {
    expect(resolveApiBaseUrl("", "development")).toBe("http://127.0.0.1:8000");
    expect(resolveApiBaseUrl(" https://api.example.com/ ", "production")).toBe(
      "https://api.example.com",
    );
  });

  it("reports missing deployed API routing rather than an opaque HTML 404", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response("<!doctype html><h1>Not found</h1>", {
        status: 404,
        headers: { "Content-Type": "text/html" },
      }),
    );
    await expect(getCatalogSeasons({ baseUrl: "", fetcher })).rejects.toThrow(
      /backend.*not.*deployed|API.*route.*unavailable/i,
    );
  });

  it("rejects a non-catalog JSON payload before it can crash the workspace", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(
        new Response(JSON.stringify({ message: "frontend fallback" })),
      );
    await expect(
      getCatalogSeasons({ baseUrl: "", fetcher }),
    ).rejects.toBeInstanceOf(ApiClientError);
  });

  it("wraps malformed successful JSON as a recoverable API error", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(new Response("<!doctype html><h1>Landing page</h1>"));
    await expect(
      getCatalogSeasons({ baseUrl: "", fetcher }),
    ).rejects.toBeInstanceOf(ApiClientError);
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
