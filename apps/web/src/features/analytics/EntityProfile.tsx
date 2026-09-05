"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getEntityAnalytics } from "@/lib/api/client";
import type { EntityAnalyticsResponse } from "@/lib/api/types";

import {
  AnalyticsPage,
  BarChart,
  CoverageNote,
  EmptyState,
  MetricStrip,
  PageIntro,
  formatMetric,
} from "./AnalyticsPrimitives";

type EntityKind = "drivers" | "constructors" | "circuits";

function entityName(data: EntityAnalyticsResponse): string {
  return String(
    data.entity.driver_name ??
      data.entity.constructor_name ??
      data.entity.circuit_name ??
      "Archive entity",
  );
}

export function EntityProfile({ kind, id }: { kind: EntityKind; id: string }) {
  const [data, setData] = useState<EntityAnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getEntityAnalytics(
      kind,
      id,
      { limit: 50 },
      { signal: controller.signal },
    )
      .then(setData)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof ApiClientError
              ? reason.message
              : "Profile could not be loaded.",
          );
      });
    return () => controller.abort();
  }, [id, kind]);

  if (error)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow={`${kind} / ${id}`} title="Record unavailable" />
        <EmptyState>{error}</EmptyState>
      </AnalyticsPage>
    );
  if (!data)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow={`${kind} / ${id}`} title="Loading record" />
        <EmptyState>Resolving the canonical entity…</EmptyState>
      </AnalyticsPage>
    );
  const name = entityName(data);
  const breakdown =
    kind === "drivers"
      ? data.circuits
      : kind === "constructors"
        ? data.drivers
        : data.drivers;
  async function loadMoreRaces() {
    if (!data || loadingMore || data.races.items.length >= data.races.total)
      return;
    setLoadingMore(true);
    try {
      const next = await getEntityAnalytics(kind, id, {
        offset: data.races.items.length,
        limit: 50,
      });
      setData((current) =>
        current
          ? {
              ...current,
              races: {
                ...next.races,
                items: [...current.races.items, ...next.races.items],
              },
            }
          : current,
      );
    } catch (reason: unknown) {
      setError(
        reason instanceof ApiClientError
          ? reason.message
          : "More race history could not be loaded.",
      );
    } finally {
      setLoadingMore(false);
    }
  }
  return (
    <AnalyticsPage>
      <PageIntro eyebrow={`${kind.slice(0, -1)} profile / ${id}`} title={name}>
        <p>
          {formatMetric(data.entity.active_start ?? data.entity.first_season)}—
          {formatMetric(data.entity.active_end ?? data.entity.latest_season)} ·
          Provider-stable identity
        </p>
      </PageIntro>
      <MetricStrip
        metrics={
          kind === "circuits"
            ? [
                { label: "Races", value: data.summary.race_count },
                { label: "Winners", value: data.summary.different_winners },
                { label: "Drivers", value: data.summary.driver_count },
                {
                  label: "Median provider pit duration (ms)",
                  value: data.summary.median_provider_pit_duration_ms,
                },
              ]
            : [
                { label: "Starts", value: data.summary.starts },
                { label: "Wins", value: data.summary.wins },
                { label: "Podiums", value: data.summary.podiums },
                { label: "Points", value: data.summary.points },
                {
                  label: "Average comparable finish",
                  value: data.summary.average_finish,
                },
                {
                  label: "Comparable finishes",
                  value: data.summary.average_finish_samples,
                },
                { label: "DNFs", value: data.summary.dnf },
              ]
        }
      />

      {data.seasons?.length ? (
        <section className="analytics-section analytics-table-section">
          <header>
            <p className="eyebrow">Longitudinal record</p>
            <h2>Season history</h2>
          </header>
          <div className="analytics-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Season</th>
                  <th>Starts</th>
                  <th>Wins</th>
                  <th>Podiums</th>
                  <th>Points</th>
                  <th>Avg comparable finish</th>
                </tr>
              </thead>
              <tbody>
                {data.seasons.map((season) => (
                  <tr key={String(season.season)}>
                    <th>
                      <Link
                        href={`/app/analytics/seasons/${String(season.season)}`}
                      >
                        {String(season.season)}
                      </Link>
                    </th>
                    <td>{formatMetric(season.starts)}</td>
                    <td>{formatMetric(season.wins)}</td>
                    <td>{formatMetric(season.podiums)}</td>
                    <td>{formatMetric(season.points)}</td>
                    <td>{formatMetric(season.average_finish)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {breakdown?.length ? (
        <section className="analytics-section">
          <header>
            <p className="eyebrow">Performance distribution</p>
            <h2>
              {kind === "drivers"
                ? "Most raced circuits"
                : kind === "constructors"
                  ? "Driver history"
                  : "Driver records"}
            </h2>
          </header>
          <BarChart
            label="Starts in this selected archive range"
            rows={breakdown.slice(0, 10).map((item) => ({
              label: String(
                item.driver_name ??
                  item.circuit_name ??
                  item.entity_name ??
                  "Unknown",
              ),
              value: Number(item.starts ?? 0),
            }))}
          />
        </section>
      ) : null}

      <section className="analytics-section race-register">
        <header>
          <p className="eyebrow">Classification ledger</p>
          <h2>Race history</h2>
        </header>
        <div>
          {data.races.items.map((race) => (
            <Link
              key={String(race.session_id)}
              href={`/app/analytics/races/${String(race.session_id)}`}
            >
              <span>
                {String(race.season)} · R
                {String(race.round_number).padStart(2, "0")}
              </span>
              <strong>{String(race.event_name)}</strong>
              <small>
                {race.finish_position
                  ? `Finish P${String(race.finish_position)}`
                  : `${formatMetric(race.wins)} wins / ${formatMetric(race.points)} pts`}
              </small>
            </Link>
          ))}
        </div>
        {data.races.total > data.races.items.length ? (
          <button
            className="analytics-load-more"
            type="button"
            disabled={loadingMore}
            onClick={() => void loadMoreRaces()}
          >
            {loadingMore
              ? "Loading more races…"
              : `Load next races · ${data.races.items.length} of ${data.races.total}`}
          </button>
        ) : null}
      </section>

      <CoverageNote coverage={data.coverage.results} />
      {kind !== "circuits" ? (
        <p className="era-warning">
          Points are source-recorded Grand Prix race-session points. Sprint
          sessions are outside this archive, scoring systems vary by era, and
          “starts” excludes only provider-explicit non-starts; ambiguous
          zero-lap entries retain provider result semantics.
        </p>
      ) : null}
      {data.coverage.pits?.race_count ? (
        <>
          <CoverageNote coverage={data.coverage.pits} />
          <p className="era-warning">
            Provider-reported pit-stop duration is not independently observed
            stationary service and is not effective pit loss.
          </p>
        </>
      ) : (
        <p className="era-warning">
          No pit-stop records are available for this selected range.
        </p>
      )}
    </AnalyticsPage>
  );
}
