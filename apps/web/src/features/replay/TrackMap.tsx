import { memo, useMemo } from "react";

import type { DriverState, TrackPosition } from "@/lib/api/types";

import {
  createTrackProjection,
  driverLabel,
  latestPositionsAt,
} from "./replay-utils";

type Props = {
  trace: TrackPosition[];
  positions: TrackPosition[];
  cursorMs: number;
  drivers: DriverState[];
  selectedDriverId: string | null;
  loading: boolean;
  availability?: string;
  errorMessage?: string | null;
};

export const TrackMap = memo(function TrackMap({
  trace,
  positions,
  cursorMs,
  drivers,
  selectedDriverId,
  loading,
  availability = "available",
  errorMessage = null,
}: Props) {
  const projection = useMemo(() => createTrackProjection(trace), [trace]);
  const latest = useMemo(
    () => latestPositionsAt(positions, cursorMs),
    [positions, cursorMs],
  );
  const driverMap = useMemo(
    () => new Map(drivers.map((driver) => [driver.driver_id, driver])),
    [drivers],
  );

  return (
    <section className="ops-panel track-panel" aria-labelledby="track-title">
      <header className="ops-panel__head">
        <div>
          <span className="panel-index">02</span>
          <h2 id="track-title">Circuit position</h2>
        </div>
        <span>
          {loading
            ? "Sampling…"
            : errorMessage
              ? "Positions unavailable"
              : `${latest.size} located`}
        </span>
      </header>
      <div className="track-stage">
        <div className="track-stage__grid" aria-hidden="true" />
        {projection ? (
          <svg
            viewBox={`0 0 ${projection.width} ${projection.height}`}
            role="img"
            aria-label={`Track map with ${latest.size} current driver positions`}
          >
            <path
              className="track-path track-path--underlay"
              d={projection.path}
            />
            <path className="track-path" d={projection.path} />
            {[...latest.values()].map((point) => {
              const projected = projection.project(point);
              const driver = driverMap.get(point.driver_id);
              const selected = point.driver_id === selectedDriverId;
              return (
                <g
                  className={
                    selected ? "car-marker car-marker--selected" : "car-marker"
                  }
                  transform={`translate(${projected.x} ${projected.y})`}
                  key={point.driver_id}
                >
                  <circle r={selected ? 12 : 7} />
                  <text y="3">
                    {driver ? driverLabel(driver).slice(0, 3) : "•"}
                  </text>
                </g>
              );
            })}
          </svg>
        ) : (
          <div className="panel-empty">
            <strong>
              {availability === "available"
                ? "Track geometry unavailable"
                : "Track position data unavailable"}
            </strong>
            <span>
              {errorMessage ??
                (availability === "available"
                  ? "A canonical full-lap position trace is required."
                  : `Canonical availability: ${availability}.`)}
            </span>
          </div>
        )}
        {projection && errorMessage && (
          <div className="track-stage__notice" role="status">
            {errorMessage}
          </div>
        )}
        <div className="track-stage__meta">
          <span>XY / canonical metres</span>
          <span>Latest sample ≤ cursor</span>
        </div>
      </div>
    </section>
  );
});
