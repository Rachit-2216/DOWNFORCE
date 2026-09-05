"use client";

import { useEffect, useRef, useState } from "react";

import { ApiClientError, getIntelligence } from "@/lib/api/client";
import type { DriverState, IntelligenceResponse } from "@/lib/api/types";

import { driverLabel, formatLapTime } from "./replay-utils";

type IntelligenceState =
  | { kind: "idle" }
  | { kind: "error"; key: string; message: string }
  | { kind: "ready"; key: string; data: IntelligenceResponse };

function describeReason(reason: string | null): string {
  if (!reason) return "Historical intelligence is unavailable at this cursor.";
  return reason.replaceAll("_", " ");
}

function signedMilliseconds(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${(Math.abs(value) / 1_000).toFixed(3)}s`;
}

export function IntelligencePanel({
  sessionId,
  driver,
  cursorMs,
}: {
  sessionId: string;
  driver: DriverState | null;
  cursorMs: number;
}) {
  const [state, setState] = useState<IntelligenceState>({ kind: "idle" });
  const sequence = useRef(0);
  const requestKey = driver
    ? `${driver.driver_id}:${Math.round(cursorMs)}`
    : null;

  useEffect(() => {
    if (!driver || !requestKey) return;
    const request = ++sequence.current;
    const controller = new AbortController();
    void getIntelligence(sessionId, driver.driver_id, cursorMs, {
      signal: controller.signal,
    })
      .then((data) => {
        if (request === sequence.current)
          setState({ kind: "ready", key: requestKey, data });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || request !== sequence.current) return;
        setState({
          kind: "error",
          key: requestKey,
          message:
            error instanceof ApiClientError
              ? error.message
              : "Historical intelligence could not be loaded.",
        });
      });
    return () => controller.abort();
  }, [cursorMs, driver, requestKey, sessionId]);

  const name = driver ? driverLabel(driver) : "No driver selected";
  const visibleKind =
    requestKey === null
      ? "idle"
      : state.kind !== "idle" && state.key === requestKey
        ? state.kind
        : "loading";
  return (
    <section
      className="ops-panel intelligence-panel"
      aria-labelledby="intelligence-title"
      aria-busy={visibleKind === "loading"}
    >
      <header className="ops-panel__head">
        <div>
          <span className="panel-index">04</span>
          <h2 id="intelligence-title">ML intelligence</h2>
        </div>
        <span>Observed / predicted · {name}</span>
      </header>
      {visibleKind === "idle" && (
        <div className="panel-empty">
          Select a driver to inspect historical intelligence.
        </div>
      )}
      {visibleKind === "loading" && (
        <div className="panel-empty" role="status">
          Reconciling predictions to the completed lap boundary…
        </div>
      )}
      {visibleKind === "error" && state.kind === "error" && (
        <div className="panel-empty" role="alert">
          {state.message}
        </div>
      )}
      {visibleKind === "ready" &&
        state.kind === "ready" &&
        state.data.availability === "unavailable" && (
          <div className="intelligence-unavailable" role="status">
            <strong>Prediction unavailable at this cursor</strong>
            <span>{describeReason(state.data.reason)}</span>
            <small>
              Canonical replay remains fully available while intelligence is
              unavailable.
            </small>
          </div>
        )}
      {visibleKind === "ready" &&
        state.kind === "ready" &&
        state.data.availability === "available" && (
          <IntelligenceReadout data={state.data} />
        )}
    </section>
  );
}

function IntelligenceReadout({ data }: { data: IntelligenceResponse }) {
  if (!data.pace || !data.tyre_degradation || !data.pit_loss) return null;
  const pace = data.pace;
  const tyre = data.tyre_degradation;
  const pit = data.pit_loss;
  const maximumDelta = Math.max(
    1,
    ...tyre.curve.map((point) => point.predicted_pace_delta_ms),
  );
  return (
    <div className="intelligence-grid">
      <article>
        <p className="intelligence-kicker">Pace · predicted</p>
        <strong className="intelligence-primary">
          {formatLapTime(pace.predicted_lap_time_ms)}
        </strong>
        <span>{pace.label}</span>
        <dl>
          <div>
            <dt>Observed latest</dt>
            <dd>{formatLapTime(pace.observed_latest_lap_time_ms)}</dd>
          </div>
          <div>
            <dt>80% interval</dt>
            <dd>
              {formatLapTime(pace.interval_80.lower_ms)}–
              {formatLapTime(pace.interval_80.upper_ms)}
            </dd>
          </div>
        </dl>
      </article>
      <article>
        <p className="intelligence-kicker">Tyre degradation · predicted</p>
        <strong className="intelligence-primary">
          {signedMilliseconds(tyre.predicted_residual_ms)}
        </strong>
        <span>
          {tyre.compound} · age {Math.floor(tyre.current_tyre_age_laps)} ·
          conditions held constant
        </span>
        <ol
          className="degradation-curve"
          aria-label="Five-lap predicted tyre pace delta"
        >
          {tyre.curve.map((point) => (
            <li key={point.laps_ahead}>
              <small>+{point.laps_ahead}L</small>
              <i
                aria-hidden="true"
                style={{
                  width: `${Math.max(3, (point.predicted_pace_delta_ms / maximumDelta) * 100)}%`,
                }}
              />
              <b>{signedMilliseconds(point.predicted_pace_delta_ms)}</b>
            </li>
          ))}
        </ol>
      </article>
      <article>
        <p className="intelligence-kicker">Pit cycle · estimated</p>
        <strong className="intelligence-primary">
          {signedMilliseconds(pit.estimated_effective_loss_ms)}
        </strong>
        <span>{pit.label}</span>
        <dl>
          <div>
            <dt>80% interval</dt>
            <dd>
              {signedMilliseconds(pit.interval_80.lower_ms)}–
              {signedMilliseconds(pit.interval_80.upper_ms)}
            </dd>
          </div>
          <div>
            <dt>Stationary time</dt>
            <dd>Not observed</dd>
          </div>
        </dl>
      </article>
      <p className="intelligence-disclaimer">
        Historical estimates only · no strategy recommendation · calibrated
        empirical uncertainty · as of lap {data.as_of.lap ?? "—"}
      </p>
    </div>
  );
}
