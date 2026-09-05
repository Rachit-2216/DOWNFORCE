"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiClientError,
  getCatalogEvents,
  getCatalogSeasons,
} from "@/lib/api/client";
import type { CatalogEvent, CatalogSeasonListResponse } from "@/lib/api/types";

type LoadState =
  | { kind: "loading" }
  | { kind: "ready"; events: CatalogEvent[]; total: number }
  | { kind: "error"; message: string };

const CAPABILITY_FILTERS = [
  ["", "All coverage"],
  ["lap_times", "Lap times"],
  ["pit_stops", "Pit stops"],
  ["compounds", "Tyres & stints"],
  ["weather", "Weather"],
  ["track_positions", "Track position"],
  ["telemetry", "Telemetry"],
  ["ml_intelligence", "ML intelligence"],
] as const;

function errorMessage(error: unknown): string {
  return error instanceof ApiClientError
    ? error.message
    : "The historical catalog could not be loaded.";
}

function readableTier(value: string): string {
  return value.replaceAll("_", " ");
}

function seasonFromUrl(seasons: CatalogSeasonListResponse): number | null {
  const raw = new URLSearchParams(window.location.search).get("season");
  if (raw === "all") return null;
  const fromUrl = raw === null ? null : Number(raw);
  return seasons.items.some((item) => item.year === fromUrl)
    ? fromUrl
    : (seasons.items[0]?.year ?? null);
}

function updateSeasonUrl(season: number | null) {
  const url = new URL(window.location.href);
  if (season === null) url.searchParams.set("season", "all");
  else url.searchParams.set("season", String(season));
  window.history.pushState(null, "", url);
}

export function ExploreArchive() {
  const [seasons, setSeasons] = useState<CatalogSeasonListResponse | null>(
    null,
  );
  const [selectedSeason, setSelectedSeason] = useState<
    number | null | undefined
  >(undefined);
  const [query, setQuery] = useState("");
  const [capability, setCapability] = useState("");
  const [status, setStatus] = useState("");
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [requestKey, setRequestKey] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<string | null>(null);
  const requestGeneration = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    void getCatalogSeasons({ signal: controller.signal })
      .then((response) => {
        setSeasons(response);
        setSelectedSeason(seasonFromUrl(response));
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({ kind: "error", message: errorMessage(error) });
      });
    return () => controller.abort();
  }, [requestKey]);

  useEffect(() => {
    if (seasons === null) return;
    const onPopState = () => {
      setSelectedSeason(seasonFromUrl(seasons));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [seasons]);

  useEffect(() => {
    if (selectedSeason === undefined || seasons === null) return;
    const generation = ++requestGeneration.current;
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => {
        setState({ kind: "loading" });
        setLoadingMore(false);
        setLoadMoreError(null);
        void getCatalogEvents(
          {
            season: selectedSeason ?? undefined,
            query: query.trim() || undefined,
            capability: capability || undefined,
            status: status || undefined,
            limit: 100,
          },
          { signal: controller.signal },
        )
          .then((response) => {
            if (generation === requestGeneration.current)
              setState({
                kind: "ready",
                events: response.items,
                total: response.total,
              });
          })
          .catch((error: unknown) => {
            if (!controller.signal.aborted)
              setState({ kind: "error", message: errorMessage(error) });
          });
      },
      query ? 180 : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [capability, query, requestKey, seasons, selectedSeason, status]);

  const selectSeason = useCallback((year: number | null) => {
    setSelectedSeason(year);
    updateSeasonUrl(year);
  }, []);

  const loadMore = useCallback(() => {
    if (state.kind !== "ready" || state.events.length >= state.total) return;
    const generation = requestGeneration.current;
    setLoadingMore(true);
    setLoadMoreError(null);
    void getCatalogEvents({
      season: selectedSeason ?? undefined,
      query: query.trim() || undefined,
      capability: capability || undefined,
      status: status || undefined,
      offset: state.events.length,
      limit: 100,
    })
      .then((response) => {
        if (generation !== requestGeneration.current) return;
        setState((current) =>
          current.kind === "ready"
            ? {
                kind: "ready",
                events: [...current.events, ...response.items],
                total: response.total,
              }
            : current,
        );
      })
      .catch((error: unknown) => {
        if (generation === requestGeneration.current)
          setLoadMoreError(errorMessage(error));
      })
      .finally(() => {
        if (generation === requestGeneration.current) setLoadingMore(false);
      });
  }, [capability, query, selectedSeason, state, status]);

  const fullCoverageCount = useMemo(() => {
    if (state.kind !== "ready") return 0;
    return state.events.filter(
      (event) => event.sessions[0]?.capability_tier === "full_downforce",
    ).length;
  }, [state]);

  return (
    <main className="explore-shell" id="main-content">
      <header className="explore-intro">
        <p className="eyebrow">Historical F1 data platform / 2000–present</p>
        <h1>Every race. Only the data that actually exists.</h1>
        <p>
          Browse results, laps, pit stops, detailed timing and full DOWNFORCE
          replays through one capability-aware archive.
        </p>
        <dl>
          <div>
            <dt>Completed races</dt>
            <dd>{seasons?.completed_event_count ?? "Loading"}</dd>
          </div>
          <div>
            <dt>Latest verified</dt>
            <dd>{seasons?.latest_completed_event_date ?? "Loading"}</dd>
          </div>
          <div>
            <dt>Detailed ML corpus</dt>
            <dd>13 races / V1 frozen</dd>
          </div>
        </dl>
      </header>

      <section className="explore-controls" aria-label="Archive filters">
        <label>
          <span>Search race, circuit, driver or team</span>
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="e.g. Schumacher, Ferrari, Silverstone"
          />
        </label>
        <label>
          <span>Required data</span>
          <select
            value={capability}
            onChange={(event) => setCapability(event.target.value)}
          >
            {CAPABILITY_FILTERS.map(([value, label]) => (
              <option value={value} key={value || "all"}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Event status</span>
          <select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
          >
            <option value="">Completed & upcoming</option>
            <option value="completed">Completed only</option>
            <option value="upcoming">Upcoming only</option>
          </select>
        </label>
      </section>

      <div className="archive-browser">
        <aside className="season-rail" aria-label="Seasons">
          <button
            key="all-seasons"
            type="button"
            className={selectedSeason === null ? "is-active" : ""}
            aria-pressed={selectedSeason === null}
            onClick={() => selectSeason(null)}
          >
            <span>All</span>
            <small>26+ seasons</small>
          </button>
          {seasons?.items.map((season) => (
            <button
              type="button"
              className={selectedSeason === season.year ? "is-active" : ""}
              aria-pressed={selectedSeason === season.year}
              onClick={() => selectSeason(season.year)}
              key={season.year}
            >
              <span>{season.year}</span>
              <small>
                {season.completed_event_count}/{season.event_count} complete
              </small>
            </button>
          ))}
        </aside>

        <section className="event-register">
          <header>
            <div>
              <p className="section-kicker">Race archive</p>
              <h2>{selectedSeason ?? "All seasons"}</h2>
              {selectedSeason ? (
                <Link href={`/app/analytics/seasons/${selectedSeason}`}>
                  View season analytics
                </Link>
              ) : null}
            </div>
            {state.kind === "ready" && (
              <span role="status" aria-live="polite" aria-atomic="true">
                {state.total} matches / {fullCoverageCount} full DOWNFORCE
                loaded
              </span>
            )}
          </header>
          {state.kind === "loading" && (
            <div className="catalog-state" role="status">
              Reading the catalog index…
            </div>
          )}
          {state.kind === "error" && (
            <div className="catalog-state catalog-state--error" role="alert">
              <strong>Catalog unavailable</strong>
              <span>{state.message}</span>
              <button
                type="button"
                onClick={() => setRequestKey((value) => value + 1)}
              >
                Retry archive
              </button>
            </div>
          )}
          {state.kind === "ready" && state.events.length === 0 && (
            <div className="catalog-state">
              No races match these evidence requirements.
            </div>
          )}
          {state.kind === "ready" && (
            <>
              <ol className="event-list">
                {state.events.map((event) => {
                  const session = event.sessions[0];
                  if (!session) return null;
                  return (
                    <li key={event.event_id}>
                      <Link
                        href={`/app/events/${event.event_id}`}
                        prefetch={false}
                      >
                        <span className="event-list__round">
                          R{String(event.round_number).padStart(2, "0")}
                        </span>
                        <span className="event-list__identity">
                          <strong>{event.name}</strong>
                          <small>
                            {event.circuit_name} / {event.event_date}
                          </small>
                        </span>
                        <span
                          className={`quality-tag quality-tag--${session.quality.status}`}
                        >
                          {session.quality.status}
                        </span>
                        <span className="tier-tag">
                          {readableTier(session.capability_tier)}
                        </span>
                        <span
                          className={`event-status event-status--${event.status}`}
                        >
                          {event.status}
                        </span>
                        <b aria-hidden="true">↗</b>
                      </Link>
                    </li>
                  );
                })}
              </ol>
              <footer className="event-register__pagination">
                <span>
                  Showing {state.events.length} of {state.total} matches
                </span>
                {state.events.length < state.total && (
                  <button
                    type="button"
                    disabled={loadingMore}
                    onClick={loadMore}
                  >
                    {loadingMore ? "Loading races…" : "Load more races"}
                  </button>
                )}
                {loadMoreError && <span role="alert">{loadMoreError}</span>}
              </footer>
            </>
          )}
        </section>
      </div>
    </main>
  );
}
