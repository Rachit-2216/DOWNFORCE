"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getSeasonAnalytics } from "@/lib/api/client";
import type { SeasonAnalyticsResponse } from "@/lib/api/types";

import {
  AnalyticsPage,
  CoverageNote,
  EmptyState,
  LineChart,
  MetricStrip,
  PageIntro,
  formatMetric,
} from "./AnalyticsPrimitives";

const seasonViews = [
  "overview",
  "drivers",
  "constructors",
  "races",
  "trends",
] as const;
type SeasonView = (typeof seasonViews)[number];

function viewFromLocation(): SeasonView {
  if (typeof window === "undefined") return "overview";
  const value = new URLSearchParams(window.location.search).get("view");
  return seasonViews.includes(value as SeasonView)
    ? (value as SeasonView)
    : "overview";
}

export function SeasonAnalytics({ year }: { year: number }) {
  const [data, setData] = useState<SeasonAnalyticsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<SeasonView>("overview");

  useEffect(() => {
    const controller = new AbortController();
    void getSeasonAnalytics(year, { signal: controller.signal })
      .then(setData)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setError(
            reason instanceof ApiClientError
              ? reason.message
              : "Season analytics could not be loaded.",
          );
      });
    return () => controller.abort();
  }, [year]);

  useEffect(() => {
    const updateFromUrl = () => setView(viewFromLocation());
    updateFromUrl();
    window.addEventListener("popstate", updateFromUrl);
    return () => window.removeEventListener("popstate", updateFromUrl);
  }, []);

  function chooseView(next: SeasonView) {
    const url = new URL(window.location.href);
    if (next === "overview") url.searchParams.delete("view");
    else url.searchParams.set("view", next);
    window.history.pushState(null, "", url);
    setView(next);
  }

  if (error)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow="Season analytics" title={String(year)} />
        <EmptyState>{error}</EmptyState>
      </AnalyticsPage>
    );
  if (!data)
    return (
      <AnalyticsPage>
        <PageIntro eyebrow="Season analytics" title={String(year)} />
        <EmptyState>Building the season record…</EmptyState>
      </AnalyticsPage>
    );

  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow="Season intelligence / Recorded Grand Prix points"
        title={`${year} season`}
      >
        <p>
          Final classifications, cumulative race-session points by round and
          capability-aware evidence.
        </p>
      </PageIntro>

      <nav className="analytics-subnav" aria-label="Season analytics views">
        {seasonViews.map((item) => (
          <button
            key={item}
            type="button"
            aria-pressed={view === item}
            onClick={() => chooseView(item)}
          >
            {item}
          </button>
        ))}
      </nav>

      {view === "overview" ? (
        <section className="analytics-view" aria-label="Season overview">
          <MetricStrip
            metrics={[
              { label: "Completed races", value: data.summary.completed_races },
              { label: "Drivers", value: data.summary.driver_count },
              { label: "Constructors", value: data.summary.constructor_count },
              {
                label: "Different winners",
                value: data.competitiveness.different_winners,
              },
              { label: "Pit stops", value: data.summary.pit_stops },
            ]}
          />
          <section className="analytics-section">
            <header>
              <p className="eyebrow">Descriptive competitiveness</p>
              <h2>Season shape</h2>
            </header>
            <MetricStrip
              metrics={[
                {
                  label: "Podium finishers",
                  value: data.competitiveness.different_podium_finishers,
                },
                {
                  label: "Driver race-points spread",
                  value: data.competitiveness.driver_points_spread,
                },
                {
                  label: "Constructor points concentration",
                  value: data.competitiveness.constructor_points_concentration,
                },
              ]}
            />
            <p className="metric-definition">
              Driver race-points spread is leader minus lowest recorded total.
              Constructor concentration is the sum of squared race-points shares
              (HHI); a higher value means points are held by fewer teams.
            </p>
          </section>
          <CoverageNote coverage={data.coverage.results} />
          <CoverageNote coverage={data.coverage.laps} />
          <CoverageNote coverage={data.coverage.pits} />
          <p className="era-warning">
            Points are source-recorded Grand Prix race-session points. Sprint
            sessions are outside this archive and scoring systems differ by era.
            Pit metrics include only races with provider pit records.
          </p>
        </section>
      ) : null}

      {view === "drivers" ? (
        <section
          className="analytics-section analytics-table-section analytics-view"
          aria-label="Season drivers"
        >
          <header>
            <p className="eyebrow">Recorded race-points order</p>
            <h2>Drivers</h2>
          </header>
          <div className="analytics-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pos</th>
                  <th>Driver</th>
                  <th>Starts</th>
                  <th>Wins</th>
                  <th>Podiums</th>
                  <th>Race points</th>
                  <th>Avg comparable finish</th>
                  <th>Comparable samples</th>
                </tr>
              </thead>
              <tbody>
                {data.drivers.map((driver, index) => (
                  <tr key={String(driver.driver_id)}>
                    <td>{index + 1}</td>
                    <th>
                      <Link
                        href={`/app/analytics/drivers/${String(driver.driver_id)}`}
                      >
                        {String(driver.driver_name)}
                      </Link>
                    </th>
                    <td>{formatMetric(driver.starts)}</td>
                    <td>{formatMetric(driver.wins)}</td>
                    <td>{formatMetric(driver.podiums)}</td>
                    <td>{formatMetric(driver.points)}</td>
                    <td>{formatMetric(driver.average_finish)}</td>
                    <td>{formatMetric(driver.average_finish_samples)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {view === "constructors" ? (
        <section
          className="analytics-section analytics-table-section analytics-view"
          aria-label="Season constructors"
        >
          <header>
            <p className="eyebrow">Constructor race-session record</p>
            <h2>Constructors</h2>
          </header>
          <div className="analytics-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pos</th>
                  <th>Constructor</th>
                  <th>Race starts</th>
                  <th>Driver entries</th>
                  <th>Wins</th>
                  <th>Podiums</th>
                  <th>Race points</th>
                </tr>
              </thead>
              <tbody>
                {data.constructors.map((team, index) => (
                  <tr key={String(team.constructor_id)}>
                    <td>{index + 1}</td>
                    <th>
                      <Link
                        href={`/app/analytics/constructors/${String(team.constructor_id)}`}
                      >
                        {String(team.constructor_name)}
                      </Link>
                    </th>
                    <td>{formatMetric(team.starts)}</td>
                    <td>{formatMetric(team.driver_entries)}</td>
                    <td>{formatMetric(team.wins)}</td>
                    <td>{formatMetric(team.podiums)}</td>
                    <td>{formatMetric(team.points)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {view === "races" ? (
        <section
          className="analytics-section race-register analytics-view"
          aria-label="Season races"
        >
          <header>
            <p className="eyebrow">Round register</p>
            <h2>Every race</h2>
          </header>
          <div>
            {data.races.map((race) => (
              <Link
                key={String(race.session_id)}
                href={`/app/analytics/races/${String(race.session_id)}`}
              >
                <span>R{String(race.round_number).padStart(2, "0")}</span>
                <strong>{String(race.event_name)}</strong>
                <small>
                  {race.winner_name
                    ? `Winner · ${String(race.winner_name)}`
                    : "Classification unavailable"}
                </small>
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {view === "trends" ? (
        <section className="analytics-view" aria-label="Season trends">
          <section className="analytics-section">
            <header>
              <p className="eyebrow">Top five drivers</p>
              <h2>Grand Prix points progression</h2>
            </header>
            <LineChart
              series={data.driver_points_progression.map((item) => ({
                name: item.entity_name,
                points: item.points,
              }))}
              label="Recorded cumulative driver Grand Prix points by round"
            />
          </section>
          <section className="analytics-section">
            <header>
              <p className="eyebrow">Top five constructors</p>
              <h2>Constructor race-points progression</h2>
            </header>
            <LineChart
              series={data.constructor_points_progression.map((item) => ({
                name: item.entity_name,
                points: item.points,
              }))}
              label="Recorded cumulative constructor Grand Prix points by round"
            />
          </section>
          <p className="era-warning">
            Sprint-session points are outside this race archive; these lines are
            not full championship standings in sprint eras.
          </p>
        </section>
      ) : null}
    </AnalyticsPage>
  );
}
