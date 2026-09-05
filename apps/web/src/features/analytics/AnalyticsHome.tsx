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

  useEffect(() => {
    const controller = new AbortController();
    void getCatalogSeasons({ signal: controller.signal })
      .then(setSeasons)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const latest = seasons?.items[0]?.year ?? 2026;
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow="Deterministic archive intelligence / 2000–2026"
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
          href={`/app/analytics/seasons/${latest}`}
        >
          <span>01 / Season</span>
          <strong>Explore {latest}</strong>
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
          <p>Source-stable identities without inferred historical lineage.</p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/circuits">
          <span>04 / Circuits</span>
          <strong>Venue history</strong>
          <p>Race winners, records and pit coverage by canonical circuit.</p>
        </Link>
        <Link className="analytics-entry" href="/app/analytics/compare">
          <span>05 / Compare</span>
          <strong>Common-race truth</strong>
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

      <section className="season-index">
        <header>
          <p className="eyebrow">Explore by season</p>
          <h2>{seasons?.items.length ?? "27"} seasons. One metric contract.</h2>
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
          {!seasons ? (
            <p className="analytics-empty">Loading the season register…</p>
          ) : null}
        </div>
      </section>
    </AnalyticsPage>
  );
}
