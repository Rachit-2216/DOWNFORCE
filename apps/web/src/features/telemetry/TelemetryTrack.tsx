"use client";
import { useMemo } from "react";
import { nearestIndex, signed } from "./telemetry-utils";
import type { Comparison, Corners, Microsectors } from "./types";
export function TelemetryTrack({
  comparison: c,
  corners,
  microsectors,
  cursor,
  onCursor,
  onZoom,
}: {
  comparison: Comparison;
  corners: Corners | null;
  microsectors: Microsectors | null;
  cursor: number;
  onCursor: (d: number) => void;
  onZoom: (z: [number, number]) => void;
}) {
  const geometry = useMemo(() => {
    const points = c.distance_m.map((d, i) => ({
      d,
      i,
      x:
        c.a.channel_valid_masks.x?.[i] && c.a.channels.x?.[i] !== null
          ? Number(c.a.channels.x[i])
          : null,
      y:
        c.a.channel_valid_masks.y?.[i] && c.a.channels.y?.[i] !== null
          ? Number(c.a.channels.y[i])
          : null,
    }));
    const valid = points.filter((p) => p.x !== null && p.y !== null);
    if (valid.length < 2) return null;
    const xs = valid.map((p) => p.x!),
      ys = valid.map((p) => p.y!);
    const minX = Math.min(...xs),
      minY = Math.min(...ys),
      width = Math.max(...xs) - minX,
      height = Math.max(...ys) - minY;
    if (width === 0 && height === 0) return null;
    const scale = Math.min(880 / Math.max(1, width), 420 / Math.max(1, height));
    const transform = (x: number, y: number) => [
      60 + (880 - width * scale) / 2 + (x - minX) * scale,
      35 + (420 - height * scale) / 2 + (height - (y - minY)) * scale,
    ];
    const sectors = microsectors?.sectors ?? [
      {
        index: 0,
        start_m: c.distance_m[0],
        end_m: c.distance_m.at(-1)!,
        delta_seconds: null,
      },
    ];
    const segments = sectors.map((sector) => {
      let broken = true;
      const path = points
        .filter((p) => p.d >= sector.start_m && p.d <= sector.end_m)
        .map((p) => {
          if (p.x === null || p.y === null) {
            broken = true;
            return "";
          }
          const [x, y] = transform(p.x, p.y);
          const part = `${broken ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
          broken = false;
          return part;
        })
        .join(" ");
      return { ...sector, path };
    });
    return { transform, segments };
  }, [c, microsectors]);
  const i = nearestIndex(c.distance_m, cursor);
  if (!geometry)
    return (
      <section className="tl-panel">
        <h2>Provider track coordinates unavailable</h2>
        <p>
          No invented circuit geometry is shown. Speed, inputs and
          distance-window analysis remain available when supported.
        </p>
      </section>
    );
  const { transform, segments } = geometry;
  return (
    <section className="tl-panel tl-track">
      <header className="tl-section-head">
        <div>
          <p className="tl-eyebrow">
            DERIVED · TIME-ALIGNED PROVIDER COORDINATES
          </p>
          <h2>Where the time changes</h2>
        </div>
        <span className="tl-caption">Blue: A gains · Red: B gains</span>
      </header>
      <svg
        viewBox="0 0 1000 500"
        role="img"
        aria-label="Provider-coordinate track map with A and B cursor positions and derived time-gain segments"
      >
        {segments.map((s) => (
          <path
            key={s.index}
            d={s.path}
            fill="none"
            stroke={
              s.delta_seconds === null
                ? "#596987"
                : s.delta_seconds <= 0
                  ? "#7ca7ff"
                  : "#ff5964"
            }
            strokeWidth="7"
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}
        {corners?.items
          .filter(
            (w) =>
              w.window.marker?.x !== null &&
              w.window.marker?.y !== null &&
              w.window.marker,
          )
          .map((w) => {
            const marker = w.window.marker!,
              [x, y] = transform(marker.x!, marker.y!);
            return (
              <g key={w.window.label}>
                <circle cx={x} cy={y} r="12" fill="#081126" stroke="#91a0bd" />
                <text
                  x={x}
                  y={y + 4}
                  textAnchor="middle"
                  fontSize="11"
                  fill="#f5f8ff"
                >
                  {marker.number}
                  {marker.letter}
                </text>
              </g>
            );
          })}
        {[c.a, c.b].map((s, j) => {
          if (
            !s.channel_valid_masks.x?.[i] ||
            !s.channel_valid_masks.y?.[i] ||
            s.channels.x?.[i] === null ||
            s.channels.y?.[i] === null
          )
            return null;
          const [x, y] = transform(
            Number(s.channels.x[i]),
            Number(s.channels.y[i]),
          );
          return (
            <g key={j}>
              <circle
                cx={x}
                cy={y}
                r={j === 0 ? 10 : 6}
                fill={j === 0 ? "#7ca7ff" : "#ff5964"}
                stroke="#030714"
                strokeWidth="2"
              />
              <text
                x={x + 15}
                y={y + (j === 0 ? -12 : 16)}
                fill={j === 0 ? "#7ca7ff" : "#ff5964"}
                fontSize="15"
                fontWeight="700"
              >
                {j === 0 ? "A" : "B"}
              </text>
            </g>
          );
        })}
      </svg>
      <p className="tl-caption">
        Provider coordinate shape, not a surveyed racing line. Markers identify
        provider corner windows, not physical apex positions.
      </p>
      <h3>20 derived distance microsectors · not FIA sectors</h3>
      {microsectors ? (
        <div className="tl-microsectors">
          {microsectors.sectors.map((s) => (
            <button
              key={s.index}
              className={
                s.delta_seconds === null
                  ? ""
                  : s.delta_seconds <= 0
                    ? "gain-a"
                    : "gain-b"
              }
              onClick={() => {
                onCursor((s.start_m + s.end_m) / 2);
                onZoom([s.start_m, s.end_m]);
              }}
              title={`${s.label}: ${signed(s.delta_seconds)}`}
            >
              <span>{s.index}</span>
              <small>{signed(s.delta_seconds)}</small>
            </button>
          ))}
        </div>
      ) : (
        <p className="tl-unavailable">
          Microsector calculation is unavailable for this pair.
        </p>
      )}
    </section>
  );
}
