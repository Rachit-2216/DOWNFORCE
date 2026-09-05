"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiClientError,
  getDrivers,
  getLaps,
  getRaceState,
  getSession,
  getTimeline,
  getTrackPositions,
} from "@/lib/api/client";
import type {
  Driver,
  Lap,
  RaceState,
  SessionResponse,
  TimelineEvent,
  TrackPosition,
} from "@/lib/api/types";

import { DriverInspector } from "./DriverInspector";
import { EventTimeline } from "./EventTimeline";
import { IntelligencePanel } from "./IntelligencePanel";
import { ReplayController } from "./ReplayController";
import { RaceControlPanel, WeatherPanel } from "./SignalPanels";
import { StrategyPanel } from "./StrategyPanel";
import { TimingTower } from "./TimingTower";
import { TrackMap } from "./TrackMap";
import { clamp, formatClock, longestDriverTrace } from "./replay-utils";
import { useReplayController } from "./useReplayController";

type StaticReplayData = {
  session: SessionResponse;
  drivers: Driver[];
  laps: Lap[];
  timeline: TimelineEvent[];
  trace: TrackPosition[];
  traceError: string | null;
};
type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: StaticReplayData };

type ReplayMode =
  "overview" | "replay" | "drivers" | "intelligence" | "strategy";
type ReplaySubview = "track" | "timing" | "stints" | "events" | "conditions";

const REPLAY_MODES: { value: ReplayMode; label: string }[] = [
  { value: "overview", label: "Overview" },
  { value: "replay", label: "Replay" },
  { value: "drivers", label: "Drivers" },
  { value: "intelligence", label: "Intelligence" },
  { value: "strategy", label: "Strategy" },
];
const REPLAY_SUBVIEWS: { value: ReplaySubview; label: string }[] = [
  { value: "track", label: "Track" },
  { value: "timing", label: "Timing" },
  { value: "stints", label: "Stints" },
  { value: "events", label: "Events" },
  { value: "conditions", label: "Conditions" },
];

function initialReplayMode(): ReplayMode {
  if (typeof window === "undefined") return "replay";
  const value = new URLSearchParams(window.location.search).get("mode");
  return REPLAY_MODES.some((item) => item.value === value)
    ? (value as ReplayMode)
    : "replay";
}

function initialReplaySubview(): ReplaySubview {
  if (typeof window === "undefined") return "track";
  const value = new URLSearchParams(window.location.search).get("view");
  return REPLAY_SUBVIEWS.some((item) => item.value === value)
    ? (value as ReplaySubview)
    : "track";
}

function pushReplayLocation(mode: ReplayMode, view: ReplaySubview) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", mode);
  if (mode === "replay") url.searchParams.set("view", view);
  else url.searchParams.delete("view");
  window.history.pushState(null, "", url);
}

async function loadAllTimeline(
  sessionId: string,
  signal: AbortSignal,
): Promise<TimelineEvent[]> {
  const first = await getTimeline(sessionId, { limit: 1_000 }, { signal });
  const items = [...first.items];
  for (let offset = first.items.length; offset < first.total; offset += 1_000) {
    const page = await getTimeline(
      sessionId,
      { offset, limit: 1_000 },
      { signal },
    );
    items.push(...page.items);
  }
  return items;
}

async function loadAllLaps(
  sessionId: string,
  signal: AbortSignal,
): Promise<Lap[]> {
  const first = await getLaps(sessionId, { limit: 1_000 }, { signal });
  const items = [...first.items];
  for (let offset = first.items.length; offset < first.total; offset += 1_000) {
    const page = await getLaps(sessionId, { offset, limit: 1_000 }, { signal });
    items.push(...page.items);
  }
  return items;
}

function describeError(error: unknown): string {
  return error instanceof ApiClientError
    ? error.message
    : "The replay workspace could not load canonical session data.";
}

export function ReplayWorkspace({ sessionId }: { sessionId: string }) {
  const [loadState, setLoadState] = useState<LoadState>({ kind: "loading" });
  useEffect(() => {
    const controller = new AbortController();
    void Promise.all([
      getSession(sessionId, { signal: controller.signal }),
      getDrivers(sessionId, { signal: controller.signal }),
      loadAllLaps(sessionId, controller.signal),
      loadAllTimeline(sessionId, controller.signal),
    ])
      .then(async ([session, drivers, laps, timeline]) => {
        const referenceLap = laps.find(
          (lap) =>
            lap.lap_start_time_ms !== null && lap.lap_end_time_ms !== null,
        );
        let trace: TrackPosition[] = [];
        let traceError: string | null = null;
        const trackAvailability =
          session.tables.track_positions?.availability ?? "unsupported";
        if (
          trackAvailability === "available" &&
          referenceLap?.lap_start_time_ms !== null &&
          referenceLap?.lap_end_time_ms !== null &&
          referenceLap
        ) {
          try {
            const response = await getTrackPositions(
              sessionId,
              {
                driverId: referenceLap.driver_id,
                fromMs: referenceLap.lap_start_time_ms,
                toMs: referenceLap.lap_end_time_ms,
                limit: 5_000,
              },
              { signal: controller.signal },
            );
            trace = response.items;
            if (trace.length < 2) {
              const fallback = await getTrackPositions(
                sessionId,
                {
                  fromMs: referenceLap.lap_start_time_ms,
                  toMs: referenceLap.lap_end_time_ms,
                  limit: 5_000,
                },
                { signal: controller.signal },
              );
              trace = longestDriverTrace(fallback.items);
            }
          } catch (error: unknown) {
            if (controller.signal.aborted) throw error;
            traceError = describeError(error);
          }
        }
        setLoadState({
          kind: "ready",
          data: {
            session,
            drivers: drivers.items,
            laps,
            timeline,
            trace,
            traceError,
          },
        });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted)
          setLoadState({ kind: "error", message: describeError(error) });
      });
    return () => controller.abort();
  }, [sessionId]);
  if (loadState.kind === "loading") return <ReplayLoading />;
  if (loadState.kind === "error")
    return <ReplayFailure message={loadState.message} />;
  return (
    <ReplayOperations
      data={loadState.data}
      key={loadState.data.session.session_id}
    />
  );
}

function ReplayLoading() {
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <main className="replay-gate" id="main-content" role="status">
        <span className="replay-gate__code">DF / LOAD</span>
        <div className="replay-gate__mark" aria-hidden="true" />
        <h1>Building canonical replay</h1>
        <p>
          Session metadata, timeline, laps and circuit trace are being verified.
        </p>
      </main>
    </>
  );
}
function ReplayFailure({ message }: { message: string }) {
  return (
    <>
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <main
        className="replay-gate replay-gate--error"
        id="main-content"
        role="alert"
      >
        <span className="replay-gate__code">DF / ERROR</span>
        <h1>Replay unavailable</h1>
        <p>{message}</p>
        <div className="replay-gate__actions">
          <button type="button" onClick={() => window.location.reload()}>
            Retry replay
          </button>
          <Link href="/app">Return to session registry</Link>
        </div>
      </main>
    </>
  );
}

function ReplayOperations({ data }: { data: StaticReplayData }) {
  const eventTable = data.session.tables.events;
  const minimumMs = eventTable?.min_session_time_ms ?? 0;
  const maximumMs = eventTable?.max_session_time_ms ?? 0;
  const firstCompletedLapMs = data.laps.reduce<number | null>(
    (earliest, lap) => {
      if (lap.lap_end_time_ms === null) return earliest;
      return earliest === null
        ? lap.lap_end_time_ms
        : Math.min(earliest, lap.lap_end_time_ms);
    },
    null,
  );
  const raceStartMs =
    firstCompletedLapMs ??
    data.session.tables.laps?.min_session_time_ms ??
    minimumMs;
  const rawInitial =
    typeof window === "undefined"
      ? null
      : new URLSearchParams(window.location.search).get("t");
  const initialFromUrl = rawInitial === null ? null : Number(rawInitial);
  const initialMs =
    initialFromUrl !== null && Number.isFinite(initialFromUrl)
      ? clamp(initialFromUrl, minimumMs, maximumMs)
      : raceStartMs;
  const controller = useReplayController(minimumMs, maximumMs, initialMs);
  const [raceState, setRaceState] = useState<RaceState | null>(null);
  const [stateError, setStateError] = useState<string | null>(null);
  const [stateRefreshKey, setStateRefreshKey] = useState(0);
  const [selectedDriverId, setSelectedDriverId] = useState<string | null>(null);
  const [positions, setPositions] = useState<TrackPosition[]>([]);
  const [positionError, setPositionError] = useState<string | null>(null);
  const [loadedPositionBucket, setLoadedPositionBucket] = useState<
    number | null
  >(null);
  const [mode, setMode] = useState<ReplayMode>(initialReplayMode);
  const [subview, setSubview] = useState<ReplaySubview>(initialReplaySubview);
  const stateSequence = useRef(0);
  const lapSequence = useRef(0);
  const positionSequence = useRef(0);
  const trackAvailability =
    data.session.tables.track_positions?.availability ?? "unsupported";
  const weatherAvailability =
    data.session.tables.weather?.availability ?? "unsupported";
  const raceControlAvailability =
    data.session.tables.race_control?.availability ?? "unsupported";
  const maximumLap = useMemo(
    () =>
      data.laps.reduce((maximum, lap) => Math.max(maximum, lap.lap_number), 0),
    [data.laps],
  );
  const positionBucket = Math.floor(controller.authoritativeCursorMs / 1_000);
  const stateLoading =
    raceState?.session_time_ms !== Math.round(controller.authoritativeCursorMs);
  const positionsLoading =
    trackAvailability === "available" &&
    loadedPositionBucket !== positionBucket;

  useEffect(() => {
    const onPopState = () => {
      setMode(initialReplayMode());
      setSubview(initialReplaySubview());
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    const request = ++stateSequence.current;
    const abort = new AbortController();
    void getRaceState(
      data.session.session_id,
      { timeMs: controller.authoritativeCursorMs },
      { signal: abort.signal },
    )
      .then((state) => {
        if (request !== stateSequence.current) return;
        setRaceState(state);
        setStateError(null);
        setSelectedDriverId(
          (selected) =>
            selected ??
            state.drivers.find((driver) => driver.position === 1)?.driver_id ??
            state.drivers[0]?.driver_id ??
            null,
        );
      })
      .catch((error: unknown) => {
        if (!abort.signal.aborted && request === stateSequence.current)
          setStateError(describeError(error));
      });
    return () => abort.abort();
  }, [
    controller.authoritativeCursorMs,
    data.session.session_id,
    stateRefreshKey,
  ]);

  useEffect(() => {
    if (trackAvailability !== "available") {
      return;
    }
    const request = ++positionSequence.current;
    const abort = new AbortController();
    const toMs = clamp(positionBucket * 1_000, minimumMs, maximumMs);
    void getTrackPositions(
      data.session.session_id,
      { fromMs: Math.max(minimumMs, toMs - 5_000), toMs, limit: 5_000 },
      { signal: abort.signal },
    )
      .then((response) => {
        if (request !== positionSequence.current) return;
        setPositions(response.items);
        setPositionError(null);
        setLoadedPositionBucket(positionBucket);
      })
      .catch((error: unknown) => {
        if (!abort.signal.aborted && request === positionSequence.current) {
          setPositions([]);
          setPositionError(describeError(error));
          setLoadedPositionBucket(positionBucket);
        }
      });
    return () => abort.abort();
  }, [
    data.session.session_id,
    maximumMs,
    minimumMs,
    positionBucket,
    trackAvailability,
  ]);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.set(
      "t",
      String(Math.round(controller.authoritativeCursorMs)),
    );
    window.history.replaceState(null, "", url);
  }, [controller.authoritativeCursorMs]);

  const seekToLap = useCallback(
    (lap: number) => {
      const request = ++lapSequence.current;
      controller.setPlaying(false);
      void getRaceState(data.session.session_id, { lap, phase: "end" })
        .then((state) => {
          if (request === lapSequence.current)
            controller.seek(state.session_time_ms);
        })
        .catch((error: unknown) => {
          if (request === lapSequence.current)
            setStateError(describeError(error));
        });
    },
    [controller, data.session.session_id],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target?.isContentEditable ||
        ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(
          target?.tagName ?? "",
        )
      )
        return;
      if (event.code === "Space") {
        event.preventDefault();
        controller.togglePlaying();
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        if (event.shiftKey)
          seekToLap(Math.max(1, (raceState?.reference_lap ?? 1) - 1));
        else controller.seekBy(-5_000);
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        if (event.shiftKey)
          seekToLap(Math.min(maximumLap, (raceState?.reference_lap ?? 1) + 1));
        else controller.seekBy(5_000);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [controller, maximumLap, raceState?.reference_lap, seekToLap]);

  const selectedDriver =
    raceState?.drivers.find(
      (driver) => driver.driver_id === selectedDriverId,
    ) ?? null;
  const eventName = data.session.session.event_name ?? "Historical race";
  const circuitName =
    data.session.session.circuit_name ?? "Circuit unavailable";
  return (
    <div className="replay-shell">
      <a className="skip-link" href="#main-content">
        Skip to main content
      </a>
      <header className="replay-topbar">
        <Link className="wordmark" href="/">
          DOWNFORCE
        </Link>
        <nav aria-label="Replay breadcrumb">
          <Link href="/app">Sessions</Link>
          <span>/</span>
          <strong>{eventName}</strong>
        </nav>
        <div className="replay-topbar__status">
          <i aria-hidden="true" />
          <span>{data.session.session.data_quality ?? "unknown"} data</span>
          <b>{raceState?.track_status ?? "loading"}</b>
        </div>
      </header>
      <main id="main-content">
        <div className="replay-heading">
          <div>
            <p className="eyebrow">
              Historical replay / {data.session.session.season ?? "—"} /{" "}
              {data.session.session.session_name ??
                data.session.session.session_type ??
                "Session unavailable"}
            </p>
            <h1>{eventName}</h1>
          </div>
          <dl>
            <div>
              <dt>Circuit</dt>
              <dd>{circuitName}</dd>
            </div>
            <div>
              <dt>Cursor</dt>
              <dd>{formatClock(controller.cursorMs)}</dd>
            </div>
            <div>
              <dt>Replay</dt>
              <dd>{data.session.replay_version ?? "Unavailable"}</dd>
            </div>
          </dl>
        </div>
        <nav className="detailed-mode-nav" aria-label="Workspace modes">
          {REPLAY_MODES.map((item) => (
            <button
              type="button"
              className={mode === item.value ? "is-active" : ""}
              aria-pressed={mode === item.value}
              onClick={() => {
                setMode(item.value);
                pushReplayLocation(item.value, subview);
              }}
              key={item.value}
            >
              {item.label}
            </button>
          ))}
        </nav>
        {(mode === "replay" ||
          mode === "intelligence" ||
          mode === "strategy") && (
          <ReplayController
            controller={controller}
            minimumMs={minimumMs}
            maximumMs={maximumMs}
            referenceLap={raceState?.reference_lap ?? null}
            maximumLap={maximumLap}
            trackStatus={raceState?.track_status ?? "loading"}
            onLapSeek={seekToLap}
            isStateLoading={stateLoading}
          />
        )}
        {stateError && (
          <div className="replay-inline-error" role="alert">
            <strong>State reconciliation failed.</strong>
            <span>{stateError}</span>
            <button
              type="button"
              onClick={() => {
                setStateError(null);
                setStateRefreshKey((key) => key + 1);
              }}
            >
              Retry
            </button>
          </div>
        )}
        {mode === "overview" && (
          <section className="detailed-overview">
            <article>
              <p className="eyebrow">Canonical race context</p>
              <h2>{circuitName}</h2>
              <p>
                A frozen, reproducible race reconstruction. Switch modes to work
                with replay state, drivers, causal estimates, or strategies.
              </p>
            </article>
            <dl>
              <div>
                <dt>Drivers</dt>
                <dd>{data.drivers.length}</dd>
              </div>
              <div>
                <dt>Lap records</dt>
                <dd>{data.laps.length}</dd>
              </div>
              <div>
                <dt>Timeline events</dt>
                <dd>{data.timeline.length}</dd>
              </div>
              <div>
                <dt>Track trace</dt>
                <dd>{trackAvailability}</dd>
              </div>
            </dl>
          </section>
        )}
        {mode === "replay" && (
          <>
            <nav className="detailed-subview-nav" aria-label="Replay views">
              {REPLAY_SUBVIEWS.map((item) => (
                <button
                  type="button"
                  className={subview === item.value ? "is-active" : ""}
                  aria-pressed={subview === item.value}
                  onClick={() => {
                    setSubview(item.value);
                    pushReplayLocation("replay", item.value);
                  }}
                  key={item.value}
                >
                  {item.label}
                </button>
              ))}
            </nav>
            <section
              className={`detailed-focus detailed-focus--${subview}`}
              aria-busy={stateLoading && !raceState}
            >
              {subview === "track" && (
                <>
                  <TrackMap
                    trace={data.trace}
                    positions={positions}
                    cursorMs={controller.authoritativeCursorMs}
                    drivers={raceState?.drivers ?? []}
                    selectedDriverId={selectedDriverId}
                    loading={positionsLoading}
                    availability={trackAvailability}
                    errorMessage={data.traceError ?? positionError}
                  />
                  <TimingTower
                    drivers={raceState?.drivers ?? []}
                    selectedDriverId={selectedDriverId}
                    onSelect={setSelectedDriverId}
                  />
                </>
              )}
              {subview === "timing" && (
                <>
                  <TimingTower
                    drivers={raceState?.drivers ?? []}
                    selectedDriverId={selectedDriverId}
                    onSelect={setSelectedDriverId}
                  />
                  <DriverInspector driver={selectedDriver} laps={data.laps} />
                </>
              )}
              {subview === "stints" && (
                <DriverInspector driver={selectedDriver} laps={data.laps} />
              )}
              {subview === "conditions" && (
                <>
                  <WeatherPanel
                    weather={raceState?.weather ?? null}
                    availability={weatherAvailability}
                  />
                  <RaceControlPanel
                    messages={raceState?.recent_race_control ?? []}
                    availability={raceControlAvailability}
                  />
                </>
              )}
            </section>
            {subview === "events" && (
              <EventTimeline
                events={data.timeline}
                minimumMs={minimumMs}
                maximumMs={maximumMs}
                cursorMs={controller.authoritativeCursorMs}
                drivers={data.drivers}
                onSeek={(timeMs) => {
                  controller.setPlaying(false);
                  controller.seek(timeMs);
                }}
              />
            )}
          </>
        )}
        {mode === "drivers" && (
          <section className="detailed-focus detailed-focus--drivers">
            <TimingTower
              drivers={raceState?.drivers ?? []}
              selectedDriverId={selectedDriverId}
              onSelect={setSelectedDriverId}
            />
            <DriverInspector driver={selectedDriver} laps={data.laps} />
          </section>
        )}
        {mode === "intelligence" && (
          <IntelligencePanel
            sessionId={data.session.session_id}
            driver={selectedDriver}
            cursorMs={controller.authoritativeCursorMs}
          />
        )}
        {mode === "strategy" && (
          <StrategyPanel
            sessionId={data.session.session_id}
            driver={selectedDriver}
            cursorMs={controller.authoritativeCursorMs}
            referenceLap={raceState?.reference_lap ?? null}
          />
        )}
        <footer className="replay-footer">
          <span>Space play/pause</span>
          <span>← → ±5 seconds</span>
          <span>Shift + ← → lap</span>
          <strong>Canonical HTTP state only</strong>
        </footer>
      </main>
    </div>
  );
}
