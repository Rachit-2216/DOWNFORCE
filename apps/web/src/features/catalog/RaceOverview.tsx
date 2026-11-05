"use client";

import Link from "next/link";
import { useAsyncResource } from "@/lib/use-async-resource";
import { AsyncState } from "@/components/ui/AsyncState";

import { getArchiveResults, getCatalogEvent } from "@/lib/api/client";
import type {
  ArchiveResult,
  CatalogEvent,
  RaceDataCapabilities,
} from "@/lib/api/types";

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; event: CatalogEvent; results: ArchiveResult[] };

const COVERAGE: [keyof RaceDataCapabilities, string][] = [
  ["results", "Classification"],
  ["grid", "Starting grid"],
  ["lap_times", "Lap times"],
  ["lap_positions", "Lap positions"],
  ["pit_stops", "Pit stops"],
  ["stints", "Stints"],
  ["compounds", "Tyre compounds"],
  ["weather", "Weather"],
  ["race_control", "Race control"],
  ["track_positions", "Track position"],
  ["telemetry", "Car telemetry"],
  ["ml_intelligence", "ML intelligence"],
  ["strategy_simulation", "Strategy simulation"],
];

function formatDuration(value: number | null): string {
  if (value === null) return "—";
  const seconds = Math.floor(value / 1_000);
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

export function RaceOverview({ eventId }: { eventId: string }) {
  const resource = useAsyncResource(`event:${eventId}`, async (signal) => {
    const event = await getCatalogEvent(eventId, { signal });
    const session = event.sessions[0];
    const results =
      session?.status === "completed" && session.capabilities.results
        ? await getArchiveResults(
            session.session_id,
            { limit: 100 },
            { signal },
          )
        : { items: [] };
    return { event, results: results.items };
  });
  const state: State = resource.data
    ? { kind: "ready", ...resource.data }
    : resource.error
      ? { kind: "error", message: resource.error }
      : { kind: "loading" };

  if (state.kind === "loading")
    return (
      <main className="catalog-gate" id="main-content">
        <AsyncState kind="loading" title="Loading race overview…" href="/app" />
      </main>
    );
  if (state.kind === "error")
    return (
      <main className="catalog-gate" id="main-content">
        <AsyncState
          kind="error"
          title="Race overview unavailable"
          detail={state.message}
          onRetry={resource.retry}
          href="/app"
        />
      </main>
    );

  const { event, results } = state;
  const session = event.sessions[0];
  if (!session)
    return (
      <main className="catalog-gate" id="main-content">
        <AsyncState
          kind="unavailable"
          title="No race session is published for this event"
          href="/app"
        />
      </main>
    );
  return (
    <main className="race-overview" id="main-content">
      <nav className="catalog-breadcrumb" aria-label="Breadcrumb">
        <Link href="/app">Explore</Link>
        <span>/</span>
        <strong>{event.name}</strong>
      </nav>
      <header className="race-overview__hero">
        <div>
          <p className="eyebrow">
            {event.season} / Round {event.round_number} / {event.status}
          </p>
          <h1>{event.name}</h1>
          <p>
            {event.circuit_name} ·{" "}
            {event.locality ?? event.country ?? "Location unavailable"} ·{" "}
            {event.event_date}
          </p>
        </div>
        <div className="race-overview__actions">
          {event.status === "completed" ? (
            <Link
              className="button button--secondary"
              href={`/app/analytics/races/${encodeURIComponent(session.session_id)}`}
            >
              View race analytics
            </Link>
          ) : null}
          {event.status === "completed" ? (
            <Link
              className="button button--primary"
              href={`/app/events/${event.event_id}/workspace?mode=overview`}
            >
              Open workspace ↗
            </Link>
          ) : (
            <span className="action-unavailable">
              Upcoming — no historical data yet
            </span>
          )}
          {session.legacy_session_id && (
            <Link
              className="button button--secondary"
              href={`/app/replay/${encodeURIComponent(session.legacy_session_id)}?mode=replay&view=track`}
            >
              Detailed replay
            </Link>
          )}
        </div>
      </header>

      <section className="coverage-section" aria-labelledby="coverage-title">
        <header>
          <div>
            <p className="section-kicker">Capability registry</p>
            <h2 id="coverage-title">Data coverage</h2>
          </div>
          <span className="tier-tag">
            {session.capability_tier.replaceAll("_", " ")}
          </span>
        </header>
        <ul className="coverage-list">
          {COVERAGE.map(([key, label]) => (
            <li
              className={session.capabilities[key] ? "is-available" : ""}
              key={key}
            >
              <span>{label}</span>
              <strong>
                {session.capabilities[key] ? "Available" : "Not provided"}
              </strong>
            </li>
          ))}
        </ul>
        {session.quality.reasons.length > 0 && (
          <p className="coverage-note">
            Quality note:{" "}
            {session.quality.reasons.join(", ").replaceAll("_", " ")}.
          </p>
        )}
      </section>

      <section
        className="classification-section"
        aria-labelledby="classification-title"
      >
        <header>
          <div>
            <p className="section-kicker">Observed result</p>
            <h2 id="classification-title">Classification</h2>
          </div>
          <span>{results.length} drivers</span>
        </header>
        {results.length ? (
          <div
            className="classification-table"
            role="table"
            aria-label="Race classification"
          >
            <div className="classification-table__head" role="row">
              <span role="columnheader">Pos</span>
              <span role="columnheader">Driver</span>
              <span role="columnheader">Team</span>
              <span role="columnheader">Grid</span>
              <span role="columnheader">Laps</span>
              <span role="columnheader">Time</span>
            </div>
            {results.map((result) => (
              <div role="row" key={result.driver_id}>
                <strong role="cell">{result.finish_position ?? "—"}</strong>
                <span role="cell">
                  <b>{result.driver_code ?? "—"}</b>
                  <small>
                    <Link href={`/app/analytics/drivers/${result.driver_id}`}>
                      {result.driver_name}
                    </Link>
                  </small>
                </span>
                <span role="cell">{result.team_name ?? "—"}</span>
                <span role="cell">{result.grid_position ?? "—"}</span>
                <span role="cell">{result.laps_completed ?? "—"}</span>
                <span role="cell">{formatDuration(result.total_time_ms)}</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="catalog-state">
            Classification is not available for this event.
          </div>
        )}
      </section>

      <section
        className="provenance-section"
        aria-labelledby="provenance-title"
      >
        <header>
          <p className="section-kicker">Source boundary</p>
          <h2 id="provenance-title">Provenance</h2>
        </header>
        {session.provenance.map((source) => (
          <dl key={`${source.provider}-${source.raw_sha256}`}>
            <div>
              <dt>Provider</dt>
              <dd>
                {source.provider} / {source.provider_version}
              </dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd>{source.source}</dd>
            </div>
            <div>
              <dt>Retrieved</dt>
              <dd>{source.retrieved_at_utc}</dd>
            </div>
            <div>
              <dt>Raw digest</dt>
              <dd>{source.raw_sha256.slice(0, 16)}…</dd>
            </div>
          </dl>
        ))}
      </section>
    </main>
  );
}
