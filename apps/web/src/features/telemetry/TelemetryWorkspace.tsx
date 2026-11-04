"use client";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";
import { useTelemetryData } from "./useTelemetryData";
import {
  TELEMETRY_VIEWS,
  type PreparationSettings,
  type TelemetryBundle,
} from "./types";
import { TelemetrySelectors } from "./TelemetrySelectors";
import { TelemetryCharts } from "./TelemetryCharts";
import { TelemetryTrack } from "./TelemetryTrack";
import { TelemetryCorners } from "./TelemetryCorners";
import { LapContext, TelemetryDetails } from "./TelemetryDetails";
import { numberLabel, signed } from "./telemetry-utils";

export function TelemetryWorkspace() {
  const searchParams = useSearchParams();
  const data = useTelemetryData(searchParams?.toString());
  const { selection: s, sessions, laps, bundle } = data;
  return (
    <main id="main-content" className="telemetry-lab">
      <header className="tl-intro">
        <div>
          <p className="tl-eyebrow">DOWNFORCE / TELEMETRY LAB</p>
          <h1>
            Every input.
            <br />
            <span>Every advantage.</span>
          </h1>
          <p>
            Two laps. One distance cursor. Inspect the evidence behind the time.
          </p>
        </div>
        <div className="tl-provenance">
          <span className="tl-badge">PRODUCTION CANONICAL</span>
          <span>RAW OBSERVATIONS → DERIVED COMPARISON</span>
        </div>
      </header>
      {sessions.kind === "loading" ? (
        <Status
          title="Loading canonical telemetry sessions"
          message="Reading configured production metadata. No sample data is substituted."
        />
      ) : sessions.kind === "error" ? (
        <Status
          title="Telemetry is unavailable"
          message={sessions.message}
          retry={data.retry}
          error
        />
      ) : sessions.data.length === 0 ? (
        <Status
          title={
            data.rootConfigured
              ? "No canonical telemetry sessions available"
              : "Production telemetry root is not configured"
          }
          message={
            data.rootConfigured
              ? "The production root contains no discoverable telemetry sessions. Ingest canonical observations before comparing laps."
              : "Configure the production detailed-session root before loading native observations. No sample data is substituted."
          }
          retry={data.retry}
        />
      ) : (
        <>
          <TelemetrySelectors
            selection={s}
            sessions={sessions.data}
            laps={laps}
            update={data.update}
          />
          <details className="tl-preparation">
            <summary>Advanced · explicit quality overrides</summary>
            <p>
              Off by default. Opting in does not turn partial, pit, deleted,
              generated or unreliable laps into clean observations.
            </p>
            <div>
              {(
                [
                  { key: "allow_partial", label: "Allow partial coverage" },
                  { key: "allow_pit_laps", label: "Allow pit laps" },
                  { key: "allow_deleted", label: "Allow deleted laps" },
                  {
                    key: "allow_provider_generated",
                    label: "Allow provider-generated laps",
                  },
                  { key: "allow_unreliable", label: "Allow unreliable laps" },
                ] as const
              ).map((flag) => (
                <label key={flag.key}>
                  <input
                    type="checkbox"
                    checked={data.settings[flag.key] ?? false}
                    onChange={(e) =>
                      data.setSettings({
                        ...data.settings,
                        [flag.key]: e.target.checked,
                      } as PreparationSettings)
                    }
                  />
                  {flag.label}
                </label>
              ))}
            </div>
          </details>
          {laps.kind === "error" ? (
            <Status
              title="Lap observations unavailable"
              message={laps.message}
              retry={data.retry}
              error
            />
          ) : !s.lapA || !s.lapB ? (
            <Status
              title={
                laps.kind === "loading"
                  ? "Loading observed laps"
                  : "Choose two laps to compare"
              }
              message="No eligible fastest-lap default is invented when valid observations are missing. Select an available lap explicitly."
            />
          ) : bundle.kind === "loading" ? (
            <Status
              title="Aligning canonical lap evidence"
              message="Loading native observations and bounded derived products for this exact revision…"
            />
          ) : bundle.kind === "error" ? (
            <Status
              title="Selected pair cannot be compared"
              message={bundle.message}
              retry={data.retry}
              error
            />
          ) : (
            <ReadyWorkspace
              key={`${s.session}:${s.revision}:${s.driverA}:${s.lapA}:${s.driverB}:${s.lapB}`}
              bundle={bundle.data}
              data={data}
            />
          )}
        </>
      )}
    </main>
  );
}
function Status({
  title,
  message,
  retry,
  error = false,
}: {
  title: string;
  message: string;
  retry?: () => void;
  error?: boolean;
}) {
  return (
    <section className="tl-status" role={error ? "alert" : "status"}>
      <p className="tl-eyebrow">
        {error ? "DATA STATE / FAILED OR UNSUPPORTED" : "DATA STATE"}
      </p>
      <h2>{title}</h2>
      <p>{message}</p>
      {retry && <button onClick={retry}>Retry</button>}
    </section>
  );
}
function ReadyWorkspace({
  bundle,
  data,
}: {
  bundle: TelemetryBundle;
  data: ReturnType<typeof useTelemetryData>;
}) {
  const c = bundle.comparison,
    s = data.selection,
    min = c.distance_m[0],
    max = c.distance_m.at(-1)!;
  const [requestedCursor, setCursor] = useState(min);
  const zoom = useMemo<[number, number]>(
    () =>
      s.zoom && s.zoom[0] < max && s.zoom[1] > min
        ? [Math.max(min, s.zoom[0]), Math.min(max, s.zoom[1])]
        : [min, max],
    [s.zoom, min, max],
  );
  const cursor = Math.max(zoom[0], Math.min(zoom[1], requestedCursor));
  const seek = (d: number) => setCursor(Math.max(min, Math.min(max, d)));
  const onZoom = (next: [number, number]) =>
    data.update({ ...s, zoom: next }, true);
  function focus(next: [number, number]) {
    seek((next[0] + next[1]) / 2);
    data.update({ ...s, view: "corners", zoom: next });
  }
  const currentSession =
    data.sessions.kind === "ready"
      ? data.sessions.data.find(
          (x) => x.session_id === s.session && x.revision === s.revision,
        )
      : undefined;
  const replayContext = new URLSearchParams();
  if (s.driverA && currentSession?.replay_driver_numbers?.includes(s.driverA)) {
    replayContext.set("driver", s.driverA);
    if (
      s.lapA &&
      currentSession.replay_laps_by_driver?.[s.driverA]?.includes(s.lapA)
    )
      replayContext.set("lap", String(s.lapA));
  }
  const replayQuery = replayContext.size ? `?${replayContext}` : "";
  return (
    <div className="tl-ready">
      <div className="tl-comparison-head">
        <div className="tl-pair-title">
          <span className="tl-a">
            A · {s.driverA} / L{s.lapA}
          </span>
          <span>VS</span>
          <span className="tl-b">
            B · {s.driverB} / L{s.lapB}
          </span>
        </div>
        <div className="tl-context-links">
          {currentSession?.replay_session_id ? (
            <Link
              href={`/app/replay/${encodeURIComponent(currentSession.replay_session_id)}${replayQuery}`}
            >
              Open replay ↗
            </Link>
          ) : (
            <span>
              Replay unavailable ·{" "}
              {currentSession?.replay_unavailable_reason ??
                "No verified destination for this telemetry revision."}
            </span>
          )}
          {currentSession?.race_analytics_session_id ? (
            <Link
              href={`/app/analytics/races/${encodeURIComponent(currentSession.race_analytics_session_id)}`}
            >
              {currentSession.session_type?.toLowerCase() === "race"
                ? "Race analytics"
                : "Weekend race analytics"}{" "}
              ↗
            </Link>
          ) : (
            <span>
              Race analytics unavailable ·{" "}
              {currentSession?.race_analytics_unavailable_reason ??
                "No verified completed race for this telemetry revision."}
            </span>
          )}
          <button
            onClick={() => {
              void navigator.clipboard?.writeText(window.location.href);
            }}
          >
            Copy comparison link
          </button>
        </div>
      </div>
      <div className="tl-coverage">
        <span className="tl-badge">DERIVED</span>
        <p>
          {c.finish_line_agreement_applicable
            ? "Finish-line comparison applicable"
            : "CROPPED PHYSICAL OVERLAP · not a full-lap finish-line comparison"}{" "}
          · {Math.round(min)}–{Math.round(max)} m · end Δ{" "}
          {signed(c.end_delta_seconds)}
        </p>
      </div>
      <nav className="tl-views" aria-label="Telemetry views">
        {TELEMETRY_VIEWS.map((view) => (
          <button
            key={view}
            className={s.view === view ? "active" : ""}
            aria-current={s.view === view ? "page" : undefined}
            onClick={() => data.update({ ...s, view })}
          >
            {view}
          </button>
        ))}
      </nav>
      <section
        className="tl-cursor-controls"
        aria-label="Shared distance cursor and zoom"
      >
        <div className="tl-cursor-title">
          <span className="tl-eyebrow">SHARED CURSOR</span>
          <output>
            {Math.round(cursor).toLocaleString()} <small>m</small>
          </output>
          <span className="tl-caption">
            Pointer / touch / arrow keys · all views stay aligned
          </span>
        </div>
        <label className="tl-cursor-range">
          <span className="tl-sr-only">Shared distance cursor</span>
          <input
            type="range"
            min={zoom[0]}
            max={zoom[1]}
            step="1"
            value={cursor}
            onChange={(e) => seek(Number(e.target.value))}
          />
        </label>
        <div className="tl-zoom-controls">
          <label>
            Zoom start m
            <input
              type="range"
              min={min}
              max={max - 10}
              step="1"
              value={zoom[0]}
              onChange={(e) =>
                onZoom([
                  Math.min(Number(e.target.value), zoom[1] - 10),
                  zoom[1],
                ])
              }
            />
          </label>
          <label>
            Zoom end m
            <input
              type="range"
              min={min + 10}
              max={max}
              step="1"
              value={zoom[1]}
              onChange={(e) =>
                onZoom([
                  zoom[0],
                  Math.max(Number(e.target.value), zoom[0] + 10),
                ])
              }
            />
          </label>
          <span>
            {Math.round(zoom[0])}–{Math.round(zoom[1])} m
          </span>
          <button onClick={() => data.update({ ...s, zoom: undefined }, true)}>
            Reset zoom
          </button>
        </div>
      </section>
      <div className="tl-view-content">
        {s.view === "overview" && (
          <>
            <div className="tl-metric-strip">
              <div>
                <span>OBSERVED LAP Δ</span>
                <strong>{signed(c.observed_lap_delta_seconds)}</strong>
              </div>
              <div>
                <span>ALIGNED POINTS</span>
                <strong>{c.distance_m.length.toLocaleString()}</strong>
              </div>
              <div>
                <span>EVENT / SESSION</span>
                <strong>
                  {currentSession?.event ?? "Canonical session"}
                  <small>{currentSession?.session_type ?? "—"}</small>
                </strong>
              </div>
              <div>
                <span>QUALITY FLAGS</span>
                <strong>
                  {c.flags.length}
                  <small>
                    {c.flags.length ? c.flags.join(" · ") : "None reported"}
                  </small>
                </strong>
              </div>
            </div>
            <TelemetryCharts
              comparison={c}
              view="overview"
              cursor={cursor}
              zoom={zoom}
              onCursor={seek}
              onZoom={onZoom}
            />
            <div className="tl-context-grid">
              <LapContext trace={bundle.traceA} side="A" />
              <LapContext trace={bundle.traceB} side="B" />
            </div>
          </>
        )}
        {(s.view === "performance" || s.view === "inputs") && (
          <TelemetryCharts
            comparison={c}
            view={s.view}
            cursor={cursor}
            zoom={zoom}
            onCursor={seek}
            onZoom={onZoom}
          />
        )}
        {s.view === "track" && (
          <TelemetryTrack
            comparison={c}
            corners={bundle.corners}
            microsectors={bundle.microsectors}
            cursor={cursor}
            onCursor={seek}
            onZoom={onZoom}
          />
        )}
        {s.view === "corners" && (
          <>
            <TelemetryCorners
              corners={bundle.corners}
              cursor={cursor}
              onFocus={focus}
            />
            <TelemetryCharts
              comparison={c}
              view="performance"
              cursor={cursor}
              zoom={zoom}
              onCursor={seek}
              onZoom={onZoom}
            />
          </>
        )}
        {s.view === "details" && <TelemetryDetails bundle={bundle} />}
      </div>
      {bundle.supplementalErrors.length > 0 && (
        <aside className="tl-coverage" role="status">
          <p>
            Supplemental analysis unavailable:{" "}
            {bundle.supplementalErrors.join(" · ")}
          </p>
          <button onClick={data.retry}>Retry</button>
        </aside>
      )}
      <footer className="tl-footnote">
        <span>
          REV {c.revision.slice(0, 12)} · {c.algorithm_version}
        </span>
        <span>Δ = A − B · negative favors A · BRAKE ACTIVE ≠ pressure</span>
        <span>
          DRS raw codes: unknown interpretation ·{" "}
          {numberLabel((max - min) / 1000, 2)} km physical overlap
        </span>
      </footer>
    </div>
  );
}
