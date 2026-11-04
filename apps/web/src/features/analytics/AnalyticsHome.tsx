"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { getCatalogSeasons } from "@/lib/api/client";
import type { CatalogSeasonListResponse } from "@/lib/api/types";

import { AnalyticsPage, PageIntro } from "./AnalyticsPrimitives";

export function AnalyticsHome() {
  const [seasons, setSeasons] = useState<CatalogSeasonListResponse | null>(
    null,
  );
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    void getCatalogSeasons({ signal: controller.signal })
      .then((data) => {
        if (!controller.signal.aborted) {
          setSeasons(data);
          setError(false);
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setError(true);
      });
    return () => controller.abort();
  }, [attempt]);

  const latest = seasons?.items[0]?.year;
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow="The race record / 2000–2026"
        title="Read the race record."
      >
        <p>
          Recorded points, final classifications and capability-aware lap and
          pit evidence. Every aggregate carries its denominator.
        </p>
      </PageIntro>

      <section className="analytics-entry-grid" aria-label="Analytics areas">
        <Link
          className="analytics-entry analytics-entry--primary"
          href={latest ? `/app/analytics/seasons/${latest}` : "#season-index"}
        >
          <span>01 / Season</span>
          <strong>{latest ? `Explore ${latest}` : "Explore seasons"}</strong>
          <p>
            Race-session points progression, competitiveness and every Grand
            Prix.
          </p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/drivers">
          <span>02 / Drivers</span>
          <strong>Career records</strong>
          <p>Search starts, results, circuits and constructor history.</p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/constructors">
          <span>03 / Constructors</span>
          <strong>Team performance</strong>
          <p>Follow each team’s recorded starts, results and points.</p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/circuits">
          <span>04 / Circuits</span>
          <strong>Venue history</strong>
          <p>Race winners, records and pit coverage by venue.</p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/compare">
          <span>05 / Compare</span>
          <strong>Head to head</strong>
          <p>
            Driver and constructor head-to-heads with explicit denominators.
          </p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/rankings">
          <span>06 / Rankings</span>
          <strong>Filter the record</strong>
          <p>Rank wins, points, starts and rates with minimum samples.</p>
        </Link>
      </section>

      <section className="season-index" id="season-index">
        <header>
          <p className="eyebrow">Explore by season</p>
          <h2>Explore the grid, year by year.</h2>
        </header>
        <div>
          {(seasons?.items ?? []).map((season) => (
            <Link
              key={season.year}
              href={`/app/analytics/seasons/${season.year}`}
            >
              <strong>{season.year}</strong>
              <span>{season.completed_event_count} completed races</span>
            </Link>
          ))}
          {error ? (
            <div className="analytics-empty" role="alert">
              <p>The season register is unavailable right now.</p>
              <button
                type="button"
                onClick={() => {
                  setError(false);
                  setAttempt((value) => value + 1);
                }}
              >
                Retry seasons
              </button>
            </div>
          ) : !seasons ? (
            <p className="analytics-empty" role="status">
              Loading the season register…
            </p>
          ) : null}
        </div>
      </section>
    </AnalyticsPage>
  );
}
