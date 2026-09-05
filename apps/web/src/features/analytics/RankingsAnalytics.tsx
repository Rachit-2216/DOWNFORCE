"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getAnalyticsRankings } from "@/lib/api/client";
import type { RankingAnalyticsResponse } from "@/lib/api/types";

import {
  AnalyticsPage,
  CoverageNote,
  EmptyState,
  PageIntro,
  formatMetric,
} from "./AnalyticsPrimitives";

const metrics = [
  "wins",
  "podiums",
  "points",
  "starts",
  "positions_gained",
  "average_finish",
  "dnf_rate",
  "pit_stops",
] as const;

type RankingMetric = (typeof metrics)[number];
type RankingFilters = {
  kind: "driver" | "constructor";
  metric: RankingMetric;
  minimum: 5 | 10 | 20;
  start: number;
  end: number;
};

const defaultFilters: RankingFilters = {
  kind: "driver",
  metric: "wins",
  minimum: 5,
  start: 2000,
  end: 2026,
};

function urlSeason(params: URLSearchParams, name: string, fallback: number) {
  const value = Number(params.get(name));
  return Number.isInteger(value) && value >= 2000 && value <= 2026
    ? value
    : fallback;
}

function filtersFromLocation(): RankingFilters {
  const params = new URLSearchParams(window.location.search);
  const metric = params.get("metric");
  const minimum = Number(params.get("minimum_starts"));
  return {
    kind: params.get("entity") === "constructor" ? "constructor" : "driver",
    metric: metrics.includes(metric as RankingMetric)
      ? (metric as RankingMetric)
      : defaultFilters.metric,
    minimum:
      minimum === 10 || minimum === 20 ? minimum : defaultFilters.minimum,
    start: urlSeason(params, "start_season", defaultFilters.start),
    end: urlSeason(params, "end_season", defaultFilters.end),
  };
}

function filtersKey(filters: RankingFilters) {
  return [
    filters.kind,
    filters.metric,
    filters.minimum,
    filters.start,
    filters.end,
  ].join(":");
}

function pushFilters(filters: RankingFilters) {
  const url = new URL(window.location.href);
  url.searchParams.set("entity", filters.kind);
  url.searchParams.set("metric", filters.metric);
  url.searchParams.set("minimum_starts", String(filters.minimum));
  url.searchParams.set("start_season", String(filters.start));
  url.searchParams.set("end_season", String(filters.end));
  window.history.pushState(null, "", url);
}

export function RankingsAnalytics() {
  const [filters, setFilters] = useState<RankingFilters>(defaultFilters);
  const { kind, metric, minimum, start, end } = filters;
  const requestKey = filtersKey(filters);
  const [page, setPage] = useState<{
    key: string;
    response: RankingAnalyticsResponse;
  } | null>(null);
  const [requestError, setRequestError] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [loadingMoreKey, setLoadingMoreKey] = useState<string | null>(null);

  useEffect(() => {
    const updateFromUrl = () => setFilters(filtersFromLocation());
    updateFromUrl();
    window.addEventListener("popstate", updateFromUrl);
    return () => window.removeEventListener("popstate", updateFromUrl);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getAnalyticsRankings(
      {
        entityType: kind,
        metric,
        minimumStarts: minimum,
        startSeason: start,
        endSeason: end,
        limit: 100,
      },
      { signal: controller.signal },
    )
      .then((response) => {
        if (controller.signal.aborted) return;
        setPage({ key: requestKey, response });
        setRequestError((current) =>
          current?.key === requestKey ? null : current,
        );
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setRequestError({
            key: requestKey,
            message:
              reason instanceof ApiClientError
                ? reason.message
                : "Rankings could not be loaded.",
          });
      });
    return () => controller.abort();
  }, [end, kind, metric, minimum, requestKey, start]);

  const data = page?.key === requestKey ? page.response : null;
  const error = requestError?.key === requestKey ? requestError.message : null;
  const loadingMore = loadingMoreKey === requestKey;

  function updateFilters(patch: Partial<RankingFilters>) {
    const next = { ...filters, ...patch };
    if (filtersKey(next) === requestKey) return;
    pushFilters(next);
    setFilters(next);
  }

  async function loadMore() {
    if (!data || loadingMore || data.items.length >= data.total) return;
    const pageKey = requestKey;
    setLoadingMoreKey(pageKey);
    try {
      const next = await getAnalyticsRankings({
        entityType: kind,
        metric,
        minimumStarts: minimum,
        startSeason: start,
        endSeason: end,
        offset: data.items.length,
        limit: 100,
      });
      setPage((current) =>
        current?.key === pageKey
          ? {
              key: pageKey,
              response: {
                ...next,
                items: [...current.response.items, ...next.items],
              },
            }
          : current,
      );
    } catch (reason: unknown) {
      setRequestError({
        key: pageKey,
        message:
          reason instanceof ApiClientError
            ? reason.message
            : "More rankings could not be loaded.",
      });
    } finally {
      setLoadingMoreKey((current) => (current === pageKey ? null : current));
    }
  }
  const metricLabel =
    data?.metric === "average_finish"
      ? "average comparable finish"
      : data?.metric.replaceAll("_", " ");
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow="Reusable ranking engine / Minimum sample enforced"
        title="Rankings"
      >
        <p>
          Recorded results only. Rate and average rankings never admit entries
          below the selected start threshold.
        </p>
      </PageIntro>
      <section className="analytics-filterbar analytics-filterbar--rankings">
        <label>
          <span>Entity</span>
          <select
            value={kind}
            onChange={(event) =>
              updateFilters({
                kind: event.target.value as "driver" | "constructor",
              })
            }
          >
            <option value="driver">Drivers</option>
            <option value="constructor">Constructors</option>
          </select>
        </label>
        <label>
          <span>Metric</span>
          <select
            value={metric}
            onChange={(event) =>
              updateFilters({ metric: event.target.value as RankingMetric })
            }
          >
            {metrics.map((item) => (
              <option key={item} value={item}>
                {item === "average_finish"
                  ? "average comparable finish"
                  : item.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Minimum starts</span>
          <select
            value={minimum}
            onChange={(event) =>
              updateFilters({
                minimum: Number(event.target.value) as 5 | 10 | 20,
              })
            }
          >
            <option value="5">5</option>
            <option value="10">10</option>
            <option value="20">20</option>
          </select>
        </label>
        <label>
          <span>From</span>
          <input
            type="number"
            min="2000"
            max="2026"
            value={start}
            onChange={(event) =>
              updateFilters({ start: Number(event.target.value) })
            }
          />
        </label>
        <label>
          <span>To</span>
          <input
            type="number"
            min="2000"
            max="2026"
            value={end}
            onChange={(event) =>
              updateFilters({ end: Number(event.target.value) })
            }
          />
        </label>
      </section>
      {error ? <EmptyState>{error}</EmptyState> : null}
      {data ? (
        <section className="analytics-section analytics-table-section ranking-table">
          <header>
            <p className="eyebrow">
              {data.total} qualifying {data.entity_type}s
            </p>
            <h2>{metricLabel}</h2>
          </header>
          <div className="analytics-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rank</th>
                  <th>Entity</th>
                  <th>Value</th>
                  <th>Starts</th>
                  <th>Races</th>
                  <th>Metric samples</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <tr key={String(item.entity_id)}>
                    <td>{formatMetric(item.rank)}</td>
                    <th>
                      <Link
                        href={`/app/analytics/${kind === "driver" ? "drivers" : "constructors"}/${String(item.entity_id)}`}
                      >
                        {String(item.entity_name)}
                      </Link>
                    </th>
                    <td>{formatMetric(item.value)}</td>
                    <td>{formatMetric(item.starts)}</td>
                    <td>{formatMetric(item.race_count)}</td>
                    <td>{formatMetric(item.sample_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.total > data.items.length ? (
            <button
              className="analytics-load-more"
              type="button"
              disabled={loadingMore}
              onClick={() => void loadMore()}
            >
              {loadingMore
                ? "Loading more rankings…"
                : `Load next rankings · ${data.items.length} of ${data.total}`}
            </button>
          ) : null}
          <CoverageNote coverage={data.coverage} />
          {data.metric === "points" ? (
            <p className="era-warning">
              These are recorded Grand Prix race-session points. Sprint points
              are outside this archive and scoring systems differ by era.
            </p>
          ) : null}
        </section>
      ) : (
        <EmptyState>Building ranking…</EmptyState>
      )}
    </AnalyticsPage>
  );
}
