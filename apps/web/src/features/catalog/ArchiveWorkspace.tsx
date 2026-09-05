"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ApiClientError,
  getArchiveLaps,
  getArchivePitStops,
  getArchiveResults,
  getCatalogEvent,
} from "@/lib/api/client";
import type {
  ArchiveLap,
  ArchivePitStop,
  ArchiveResult,
  CatalogEvent,
  RaceDataCapabilities,
} from "@/lib/api/types";

type WorkspaceMode =
  "overview" | "replay" | "drivers" | "intelligence" | "strategy";
type ReplayView = "track" | "timing" | "stints" | "events" | "conditions";
type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; event: CatalogEvent; results: ArchiveResult[] };

const MODES: {
  value: WorkspaceMode;
  label: string;
  capability?: keyof RaceDataCapabilities;
}[] = [
  { value: "overview", label: "Overview" },
  { value: "replay", label: "Replay" },
  { value: "drivers", label: "Drivers", capability: "results" },
  {
    value: "intelligence",
    label: "Intelligence",
    capability: "ml_intelligence",
  },
  { value: "strategy", label: "Strategy", capability: "strategy_simulation" },
];
const VIEWS: {
  value: ReplayView;
  label: string;
  capability: keyof RaceDataCapabilities;
}[] = [
  { value: "track", label: "Track", capability: "track_positions" },
  { value: "timing", label: "Timing", capability: "lap_positions" },
  { value: "stints", label: "Stints", capability: "stints" },
  { value: "events", label: "Events", capability: "race_control" },
  { value: "conditions", label: "Conditions", capability: "weather" },
];

function errorMessage(error: unknown): string {
  return error instanceof ApiClientError
    ? error.message
    : "The engineering workspace could not load this race.";
}

function initialMode(): WorkspaceMode {
  if (typeof window === "undefined") return "overview";
  const value = new URLSearchParams(window.location.search).get("mode");
  return MODES.some((item) => item.value === value)
    ? (value as WorkspaceMode)
    : "overview";
}

function initialView(): ReplayView {
  if (typeof window === "undefined") return "timing";
  const value = new URLSearchParams(window.location.search).get("view");
  return VIEWS.some((item) => item.value === value)
    ? (value as ReplayView)
    : "timing";
}

function pushWorkspaceState(
  mode: WorkspaceMode,
  view: ReplayView,
  lap: number,
) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", mode);
  if (mode === "replay") {
    url.searchParams.set("view", view);
    url.searchParams.set("lap", String(lap));
  } else {
    url.searchParams.delete("view");
    url.searchParams.delete("lap");
  }
  window.history.pushState(null, "", url);
}

function formatLapTime(value: number | null): string {
  if (value === null) return "Not provided";
  const minutes = Math.floor(value / 60_000);
  const seconds = ((value % 60_000) / 1_000).toFixed(3).padStart(6, "0");
  return `${minutes}:${seconds}`;
}

function capabilityReason(label: string): string {
  return `${label} is not available from the source data for this race.`;
}

export function ArchiveWorkspace({ eventId }: { eventId: string }) {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [mode, setMode] = useState<WorkspaceMode>(initialMode);
  const [view, setView] = useState<ReplayView>(initialView);
  const [selectedDriverId, setSelectedDriverId] = useState<string | null>(null);
  const [selectedLap, setSelectedLap] = useState(() => {
    if (typeof window === "undefined") return 1;
    const rawLap = Number(
      new URLSearchParams(window.location.search).get("lap"),
    );
    return Number.isFinite(rawLap) && rawLap >= 1 ? Math.round(rawLap) : 1;
  });
  const [driverLaps, setDriverLaps] = useState<ArchiveLap[]>([]);
  const [lapPositions, setLapPositions] = useState<ArchiveLap[]>([]);
  const [pitStops, setPitStops] = useState<ArchivePitStop[]>([]);

  useEffect(() => {
    const onPopState = () => {
      setMode(initialMode());
      setView(initialView());
      const nextLap = Number(
        new URLSearchParams(window.location.search).get("lap"),
      );
      if (Number.isFinite(nextLap) && nextLap >= 1)
        setSelectedLap(Math.round(nextLap));
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void getCatalogEvent(eventId, { signal: controller.signal })
      .then(async (event) => {
        const session = event.sessions[0];
        const response =
          session?.status === "completed"
            ? await getArchiveResults(
                session.session_id,
                { limit: 100 },
                { signal: controller.signal },
              )
            : { items: [] };
        setSelectedDriverId(response.items[0]?.driver_id ?? null);
        setState({ kind: "ready", event, results: response.items });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setState({ kind: "error", message: errorMessage(error) });
      });
    return () => controller.abort();
  }, [eventId]);

  const event = state.kind === "ready" ? state.event : null;
  const session = event?.sessions[0] ?? null;
  const maximumLap = useMemo(
    () =>
      state.kind === "ready"
        ? state.results.reduce(
            (maximum, result) => Math.max(maximum, result.laps_completed ?? 0),
            1,
          )
        : 1,
    [state],
  );

  useEffect(() => {
    if (!session || !selectedDriverId || mode !== "replay") return;
    const controller = new AbortController();
    const lapRequest = getArchiveLaps(
      session.session_id,
      { driverId: selectedDriverId, limit: 1_000 },
      { signal: controller.signal },
    );
    const pitRequest = session.capabilities.pit_stops
      ? getArchivePitStops(
          session.session_id,
          { driverId: selectedDriverId },
          { signal: controller.signal },
        )
      : Promise.resolve({ items: [] });
    void Promise.all([lapRequest, pitRequest])
      .then(([laps, pits]) => {
        setDriverLaps(laps.items);
        setPitStops(pits.items);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setDriverLaps([]);
          setPitStops([]);
        }
      });
    return () => controller.abort();
  }, [mode, selectedDriverId, session]);

  useEffect(() => {
    if (!session || mode !== "replay" || view !== "timing") return;
    const controller = new AbortController();
    void getArchiveLaps(
      session.session_id,
      { fromLap: selectedLap, toLap: selectedLap, limit: 100 },
      { signal: controller.signal },
    )
      .then((response) => setLapPositions(response.items))
      .catch(() => {
        if (!controller.signal.aborted) setLapPositions([]);
      });
    return () => controller.abort();
  }, [mode, selectedLap, session, view]);

  const selectMode = useCallback(
    (next: WorkspaceMode) => {
      setMode(next);
      pushWorkspaceState(next, view, selectedLap);
    },
    [selectedLap, view],
  );
  const selectView = useCallback(
    (next: ReplayView) => {
      setView(next);
      pushWorkspaceState("replay", next, selectedLap);
    },
    [selectedLap],
  );
  const seekLap = useCallback(
    (lap: number) => {
      const next = Math.min(maximumLap, Math.max(1, lap));
      setSelectedLap(next);
      const url = new URL(window.location.href);
      url.searchParams.set("lap", String(next));
      window.history.replaceState(null, "", url);
    },
    [maximumLap],
  );

  if (state.kind === "loading")
    return (
      <main className="catalog-gate" id="main-content" role="status">
        Opening engineering workspace…
      </main>
    );
  if (state.kind === "error")
    return (
      <main className="catalog-gate" id="main-content" role="alert">
        <strong>Workspace unavailable</strong>
        <span>{state.message}</span>
      </main>
    );
  if (!event || !session) return null;

  const replayAvailable =
    session.capabilities.lap_times || session.capabilities.lap_positions;
  const selectedResult =
    state.results.find((result) => result.driver_id === selectedDriverId) ??
    null;
  return (
    <main className="archive-workspace" id="main-content">
      <header className="workspace-heading">
        <div>
          <nav className="catalog-breadcrumb" aria-label="Breadcrumb">
            <Link href="/app">Explore</Link>
            <span>/</span>
            <Link href={`/app/events/${event.event_id}`}>{event.name}</Link>
          </nav>
          <p className="eyebrow">
            {event.season} / R{event.round_number} /{" "}
            {session.capability_tier.replaceAll("_", " ")}
          </p>
          <h1>{event.name}</h1>
        </div>
        <dl>
          <div>
            <dt>Quality</dt>
            <dd>{session.quality.status}</dd>
          </div>
          <div>
            <dt>Race data</dt>
            <dd>{session.row_counts.laps ?? 0} lap rows</dd>
          </div>
          <div>
            <dt>Source</dt>
            <dd>{session.provenance[0]?.provider ?? "Unavailable"}</dd>
          </div>
        </dl>
      </header>

      <nav className="workspace-modes" aria-label="Workspace modes">
        {MODES.map((item) => {
          const available =
            item.value === "replay"
              ? replayAvailable
              : item.capability === undefined ||
                session.capabilities[item.capability];
          return (
            <button
              type="button"
              className={mode === item.value ? "is-active" : ""}
              aria-pressed={mode === item.value}
              aria-label={
                available
                  ? item.label
                  : `${item.label} unavailable. ${capabilityReason(item.label)}`
              }
              disabled={!available}
              title={available ? undefined : capabilityReason(item.label)}
              onClick={() => selectMode(item.value)}
              key={item.value}
            >
              {item.label}
              <small>{available ? "Available" : "Unavailable"}</small>
            </button>
          );
        })}
      </nav>

      {mode === "overview" && (
        <section className="workspace-focus workspace-overview">
          <article>
            <p className="section-kicker">Race context</p>
            <h2>{event.circuit_name}</h2>
            <p>
              {event.event_date} ·{" "}
              {event.locality ?? event.country ?? "Location unavailable"}
            </p>
            <dl>
              <div>
                <dt>Drivers</dt>
                <dd>{state.results.length}</dd>
              </div>
              <div>
                <dt>Winner</dt>
                <dd>{state.results[0]?.driver_name ?? "Not provided"}</dd>
              </div>
              <div>
                <dt>Pit records</dt>
                <dd>{session.row_counts.pit_stops ?? 0}</dd>
              </div>
              <div>
                <dt>Detailed replay</dt>
                <dd>
                  {session.legacy_session_id ? "Linked" : "Not available"}
                </dd>
              </div>
            </dl>
          </article>
          <aside>
            <p className="section-kicker">Operating boundary</p>
            <h2>What you can do</h2>
            <ul>
              <li>
                {replayAvailable
                  ? "Replay lap progression"
                  : "Replay not available"}
              </li>
              <li>
                {session.capabilities.pit_stops
                  ? "Inspect pit-stop records"
                  : "Pit stops not provided"}
              </li>
              <li>
                {session.capabilities.ml_intelligence
                  ? "Open causal ML estimates"
                  : "ML V1 does not cover this race"}
              </li>
            </ul>
          </aside>
        </section>
      )}

      {mode === "drivers" && (
        <section className="workspace-focus workspace-drivers">
          <header>
            <p className="section-kicker">Observed classification</p>
            <h2>Drivers</h2>
          </header>
          <div className="classification-table">
            {state.results.map((result) => (
              <button
                type="button"
                className={
                  result.driver_id === selectedDriverId ? "is-active" : ""
                }
                onClick={() => setSelectedDriverId(result.driver_id)}
                key={result.driver_id}
              >
                <strong>{result.finish_position ?? "—"}</strong>
                <span>
                  <b>{result.driver_code ?? "—"}</b>
                  <small>{result.driver_name}</small>
                </span>
                <span>{result.team_name ?? "—"}</span>
                <span>Grid {result.grid_position ?? "—"}</span>
                <span>{result.status ?? "Status unavailable"}</span>
              </button>
            ))}
          </div>
        </section>
      )}

      {mode === "replay" && replayAvailable && (
        <>
          <nav className="replay-subviews" aria-label="Replay views">
            {VIEWS.map((item) => {
              const available = session.capabilities[item.capability];
              return (
                <button
                  type="button"
                  disabled={!available}
                  className={view === item.value ? "is-active" : ""}
                  aria-pressed={view === item.value}
                  aria-label={
                    available
                      ? item.label
                      : `${item.label} unavailable. ${capabilityReason(item.label)}`
                  }
                  title={available ? undefined : capabilityReason(item.label)}
                  onClick={() => selectView(item.value)}
                  key={item.value}
                >
                  {item.label}
                </button>
              );
            })}
          </nav>
          {view === "timing" ? (
            <section className="workspace-focus replay-timing">
              <article>
                <header>
                  <div>
                    <p className="section-kicker">Lap progression</p>
                    <h2>Timing / Lap {selectedLap}</h2>
                  </div>
                  <span>
                    {session.capabilities.lap_times
                      ? "Times + positions"
                      : "Positions only"}
                  </span>
                </header>
                <div className="lap-seeker">
                  <button
                    type="button"
                    onClick={() => seekLap(selectedLap - 1)}
                    disabled={selectedLap <= 1}
                  >
                    ←
                  </button>
                  <input
                    aria-label="Replay lap"
                    type="range"
                    min={1}
                    max={maximumLap}
                    value={selectedLap}
                    onChange={(event) => seekLap(Number(event.target.value))}
                  />
                  <button
                    type="button"
                    onClick={() => seekLap(selectedLap + 1)}
                    disabled={selectedLap >= maximumLap}
                  >
                    →
                  </button>
                  <output>
                    Lap {selectedLap} / {maximumLap}
                  </output>
                </div>
                <ol className="archive-timing-list">
                  {[...lapPositions]
                    .sort((a, b) => (a.position ?? 99) - (b.position ?? 99))
                    .map((lap) => {
                      const result = state.results.find(
                        (item) => item.driver_id === lap.driver_id,
                      );
                      return (
                        <li
                          className={
                            lap.driver_id === selectedDriverId
                              ? "is-active"
                              : ""
                          }
                          key={lap.driver_id}
                        >
                          <button
                            type="button"
                            onClick={() => setSelectedDriverId(lap.driver_id)}
                          >
                            <strong>P{lap.position ?? "—"}</strong>
                            <span>
                              <b>{result?.driver_code ?? lap.driver_id}</b>
                              <small>{result?.driver_name ?? "Driver"}</small>
                            </span>
                            <time>{formatLapTime(lap.lap_time_ms)}</time>
                          </button>
                        </li>
                      );
                    })}
                </ol>
              </article>
              <aside className="archive-driver-inspector">
                <p className="section-kicker">Driver inspector</p>
                <h2>{selectedResult?.driver_name ?? "Select a driver"}</h2>
                <p>{selectedResult?.team_name ?? "Team unavailable"}</p>
                <dl>
                  <div>
                    <dt>Grid</dt>
                    <dd>{selectedResult?.grid_position ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Finish</dt>
                    <dd>{selectedResult?.finish_position ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>Stops</dt>
                    <dd>{pitStops.length}</dd>
                  </div>
                </dl>
                <h3>Lap history</h3>
                <ol>
                  {driverLaps
                    .filter(
                      (lap) => Math.abs(lap.lap_number - selectedLap) <= 2,
                    )
                    .map((lap) => (
                      <li key={lap.lap_number}>
                        <span>L{lap.lap_number}</span>
                        <strong>P{lap.position ?? "—"}</strong>
                        <time>{formatLapTime(lap.lap_time_ms)}</time>
                      </li>
                    ))}
                </ol>
              </aside>
            </section>
          ) : (
            <DetailedHandoff event={event} view={view} />
          )}
        </>
      )}

      {(mode === "intelligence" || mode === "strategy") && (
        <DetailedHandoff event={event} view={mode} />
      )}
    </main>
  );
}

function DetailedHandoff({
  event,
  view,
}: {
  event: CatalogEvent;
  view: ReplayView | "intelligence" | "strategy";
}) {
  const legacy = event.sessions[0]?.legacy_session_id;
  return (
    <section className="workspace-handoff">
      <p className="section-kicker">Detailed canonical workspace</p>
      <h2>{view.replaceAll("_", " ")}</h2>
      <p>
        This view uses the detailed replay dataset rather than the broad archive
        tables.
      </p>
      {legacy ? (
        <Link
          className="button button--primary"
          href={`/app/replay/${encodeURIComponent(legacy)}?mode=${view === "track" || view === "stints" || view === "events" || view === "conditions" ? "replay" : view}&view=${view}`}
        >
          Continue with verified detailed data ↗
        </Link>
      ) : (
        <span className="action-unavailable">
          This race is outside the frozen 13-race detailed corpus.
        </span>
      )}
    </section>
  );
}
