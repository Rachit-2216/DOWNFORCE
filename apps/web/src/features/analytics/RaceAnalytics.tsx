"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getRaceAnalytics } from "@/lib/api/client";
import type { RaceAnalyticsResponse } from "@/lib/api/types";

import {
  AnalyticsPage,
  CoverageNote,
  EmptyState,
  LineChart,
  MetricStrip,
  PageIntro,
  formatMetric,
} from "./AnalyticsPrimitives";

export function RaceAnalytics({ sessionId }: { sessionId: string }) {
  const [data, setData] = useState<RaceAnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void getRaceAnalytics(sessionId, { signal: controller.signal })
      .then(setData)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof ApiClientError
              ? reason.message
              : "Race analytics could not be loaded.",
          );
      });
    return () => controller.abort();
  }, [sessionId]);
  if (error)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow="Race analytics" title="Unavailable" />
        <EmptyState>{error}</EmptyState>
      </AnalyticsPage>
    );
  if (!data)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow="Race analytics" title="Loading classification" />
        <EmptyState>Resolving race evidence…</EmptyState>
      </AnalyticsPage>
    );
  const winner =
    data.summary.winner &&
    typeof data.summary.winner === "object" &&
    !Array.isArray(data.summary.winner)
      ? data.summary.winner
      : null;
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow={`${formatMetric(data.event.season)} / Round ${formatMetric(data.event.round_number)} / ${formatMetric(data.event.quality_status)}`}
        title={String(data.event.event_name)}
      >
        <p>
          {formatMetric(data.event.circuit_name)} ·{" "}
          {formatMetric(data.event.event_date)}
        </p>
        <div className="intro-actions">
          <Link href={`/app/events/${String(data.event.event_id)}`}>
            Open race overview
          </Link>
          <Link href={`/app/replay/${sessionId}`}>Open replay</Link>
        </div>
      </PageIntro>
      <MetricStrip
        metrics={[
          { label: "Winner", value: winner?.driver_name },
          { label: "Drivers", value: data.summary.driver_count },
          { label: "Recorded laps", value: data.summary.recorded_laps },
          { label: "Pit stops", value: data.summary.pit_stops },
          { label: "DNFs", value: data.summary.dnf_count },
        ]}
      />
      <section className="analytics-section analytics-table-section">
        <header>
          <p className="eyebrow">Final classification</p>
          <h2>Driver result</h2>
        </header>
        <div className="analytics-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Finish</th>
                <th>Driver</th>
                <th>Constructor</th>
                <th>Grid</th>
                <th>Gain</th>
                <th>Race points</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.drivers.map((driver) => (
                <tr key={String(driver.driver_id)}>
                  <td>
                    {driver.finish_position
                      ? `P${String(driver.finish_position)}`
                      : "—"}
                  </td>
                  <th>
                    <Link
                      href={`/app/analytics/drivers/${String(driver.driver_id)}`}
                    >
                      {String(driver.driver_name)}
                    </Link>
                  </th>
                  <td>
                    {driver.constructor_id ? (
                      <Link
                        href={`/app/analytics/constructors/${String(driver.constructor_id)}`}
                      >
                        {String(driver.constructor_name)}
                      </Link>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td>{formatMetric(driver.grid_position)}</td>
                  <td>{formatMetric(driver.positions_gained)}</td>
                  <td>{formatMetric(driver.points)}</td>
                  <td>{formatMetric(driver.outcome)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section className="analytics-section">
        <header>
          <p className="eyebrow">Grid to flag / Comparable finishes only</p>
          <h2>Biggest movers</h2>
        </header>
        <ol className="mover-list">
          {data.biggest_movers.map((driver) => (
            <li key={String(driver.driver_id)}>
              <strong>{String(driver.driver_name)}</strong>
              <span>
                P{formatMetric(driver.grid_position)} → P
                {formatMetric(driver.finish_position)}
              </span>
              <b>+{formatMetric(driver.positions_gained)}</b>
            </li>
          ))}
        </ol>
      </section>
      <section className="analytics-section">
        <header>
          <p className="eyebrow">
            Recorded lap position / Selected leading finishers
          </p>
          <h2>Position progression</h2>
        </header>
        <LineChart
          label="Relative recorded position by lap; higher on the chart means a better position"
          xAxisLabel="lap"
          valuePrefix="P"
          series={data.position_progression.map((driver) => ({
            name: String(driver.driver_name),
            points: Array.isArray(driver.points)
              ? driver.points.flatMap((point) => {
                  if (
                    typeof point !== "object" ||
                    point === null ||
                    Array.isArray(point) ||
                    typeof point.lap !== "number" ||
                    typeof point.position !== "number"
                  )
                    return [];
                  return [
                    {
                      round_number: point.lap,
                      value: data.drivers.length + 1 - point.position,
                      display_value: point.position,
                    },
                  ];
                })
              : [],
          }))}
        />
      </section>
      <CoverageNote coverage={data.coverage.results} />
      <CoverageNote coverage={data.coverage.laps} />
      {data.coverage.pits.race_count ? (
        <CoverageNote coverage={data.coverage.pits} />
      ) : (
        <p className="era-warning">
          No pit-stop records are available for this race. This does not mean
          zero stops occurred.
        </p>
      )}
    </AnalyticsPage>
  );
}
