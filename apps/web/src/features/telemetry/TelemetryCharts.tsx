"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  nearestIndex,
  numberLabel,
  presentationIndices,
  signed,
} from "./telemetry-utils";
import type { ChannelValue, Comparison, TelemetryView } from "./types";
type Props = {
  comparison: Comparison;
  view: TelemetryView;
  cursor: number;
  zoom: [number, number];
  onCursor: (distance: number) => void;
  onZoom: (zoom: [number, number]) => void;
};
type Series = { values: ChannelValue[]; color: string; mask: boolean[] };
const CHANNELS = {
  delta: { title: "TIME DELTA · A − B", unit: "s", step: false },
  speed_kmh: { title: "SPEED", unit: "km/h", step: false },
  rpm: { title: "ENGINE SPEED", unit: "rpm", step: false },
  throttle_percent: { title: "THROTTLE", unit: "%", step: false },
  brake: { title: "BRAKE ACTIVE", unit: "binary", step: true },
  gear: { title: "GEAR", unit: "gear", step: true },
  drs_raw: { title: "DRS · RAW PROVIDER CODE", unit: "raw code", step: true },
};
type Channel = keyof typeof CHANNELS;
function ChannelChart({
  comparison: c,
  channel,
  cursor,
  zoom,
  onCursor,
  onZoom,
}: Props & { channel: Channel }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const wrapper = useRef<HTMLDivElement>(null);
  const raf = useRef<number | null>(null);
  const dragStart = useRef<number | null>(null);
  const [brush, setBrush] = useState(false);
  const info = CHANNELS[channel];
  const [zoomStart, zoomEnd] = zoom;
  const data = useMemo<Series[]>(
    () =>
      channel === "delta"
        ? [{ values: c.delta_seconds, color: "#f5f8ff", mask: c.valid_mask }]
        : [c.a, c.b].map((s, i) => ({
            values: s.channels[channel] ?? [],
            mask: s.channel_valid_masks[channel] ?? [],
            color: i === 0 ? "#7ca7ff" : "#ff5964",
          })),
    [c, channel],
  );
  const unavailable =
    channel !== "delta" &&
    data.every((s) => !s.values.some((v, i) => v !== null && s.mask[i]));
  const drsDefault =
    channel === "drs_raw" &&
    [c.availability_a.drs_raw, c.availability_b.drs_raw].some((v) =>
      v?.includes("parser-default"),
    );
  useEffect(() => {
    const node = canvas.current,
      container = wrapper.current;
    if (!node || !container || unavailable || drsDefault) return;
    function draw() {
      const ctx = node!.getContext("2d");
      if (!ctx) return;
      const width = Math.max(200, container!.clientWidth),
        height = 190,
        ratio = window.devicePixelRatio || 1;
      node!.width = width * ratio;
      node!.height = height * ratio;
      ctx.scale(ratio, ratio);
      ctx.clearRect(0, 0, width, height);
      const left = 52,
        right = width - 20,
        top = 15,
        bottom = 160;
      const visible = data.flatMap((s) =>
        s.values
          .filter(
            (v, i) =>
              c.distance_m[i] >= zoomStart &&
              c.distance_m[i] <= zoomEnd &&
              s.mask[i] &&
              v !== null,
          )
          .map(Number),
      );
      let min = 0,
        max = 1;
      if (visible.length) {
        min = Math.min(...visible);
        max = Math.max(...visible);
      }
      if (channel === "throttle_percent") {
        min = 0;
        max = 100;
      }
      if (channel === "brake") {
        min = 0;
        max = 1;
      }
      if (min === max) {
        min -= 0.5;
        max += 0.5;
      }
      const pad = (max - min) * 0.06;
      min -= pad;
      max += pad;
      const x = (d: number) =>
        left + ((d - zoomStart) / (zoomEnd - zoomStart)) * (right - left);
      const y = (v: number) =>
        bottom - ((v - min) / (max - min)) * (bottom - top);
      ctx.font = "11px 'JetBrains Mono', monospace";
      ctx.lineWidth = 1;
      for (let tick = 0; tick <= 4; tick++) {
        const value = min + ((max - min) * tick) / 4,
          yy = y(value);
        ctx.strokeStyle = "rgba(151,177,226,.12)";
        ctx.beginPath();
        ctx.moveTo(left, yy);
        ctx.lineTo(right, yy);
        ctx.stroke();
        ctx.fillStyle = "#91a0bd";
        ctx.textAlign = "right";
        ctx.fillText(
          value.toFixed(channel === "delta" ? 2 : 0),
          left - 8,
          yy + 4,
        );
        ctx.textAlign = tick === 0 ? "left" : tick === 4 ? "right" : "center";
        ctx.fillText(
          `${Math.round(zoomStart + ((zoomEnd - zoomStart) * tick) / 4)} m`,
          left + ((right - left) * tick) / 4,
          184,
        );
      }
      if (channel === "delta" && min < 0 && max > 0) {
        ctx.strokeStyle = "#596987";
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(left, y(0));
        ctx.lineTo(right, y(0));
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, top, right - left, bottom - top);
      ctx.clip();
      for (const series of data) {
        const values = series.values.map((v, i) => (series.mask[i] ? v : null));
        ctx.strokeStyle = series.color;
        ctx.lineWidth = 1.7;
        ctx.beginPath();
        let previous: { x: number; y: number } | null = null;
        for (const i of presentationIndices(
          values,
          Math.max(400, width * 2),
          info.step,
        )) {
          const value = values[i];
          if (value === null || value === undefined) {
            previous = null;
            continue;
          }
          const point = { x: x(c.distance_m[i]), y: y(Number(value)) };
          if (!previous) ctx.moveTo(point.x, point.y);
          else {
            if (info.step) ctx.lineTo(point.x, previous.y);
            ctx.lineTo(point.x, point.y);
          }
          previous = point;
        }
        ctx.stroke();
      }
      ctx.restore();
    }
    draw();
    const observer =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(draw) : null;
    observer?.observe(container);
    return () => observer?.disconnect();
  }, [
    c,
    data,
    zoomStart,
    zoomEnd,
    channel,
    info.step,
    unavailable,
    drsDefault,
  ]);
  useEffect(
    () => () => {
      if (raf.current !== null) cancelAnimationFrame(raf.current);
    },
    [],
  );
  const index = nearestIndex(c.distance_m, cursor);
  const readout =
    channel === "delta"
      ? signed(c.valid_mask[index] ? c.delta_seconds[index] : null)
      : data
          .map((s) =>
            numberLabel(
              s.mask[index] ? s.values[index] : null,
              info.step ? 0 : 1,
            ),
          )
          .join(" / ") + ` ${info.unit}`;
  function distance(clientX: number) {
    const rect = wrapper.current!.getBoundingClientRect();
    return Math.max(
      zoom[0],
      Math.min(
        zoom[1],
        zoom[0] +
          ((clientX - rect.left - 52) / Math.max(1, rect.width - 72)) *
            (zoom[1] - zoom[0]),
      ),
    );
  }
  return (
    <section className="tl-chart">
      <header>
        <h3>{info.title}</h3>
        <output>{drsDefault ? "NOT RECORDED" : readout}</output>
        <button
          type="button"
          className={brush ? "active" : ""}
          aria-pressed={brush}
          onClick={() => setBrush((v) => !v)}
        >
          Brush zoom
        </button>
      </header>
      {drsDefault ? (
        <p className="tl-unavailable">
          DRS not recorded — provider-parser default. Zero is not observed OFF.
          Other raw codes remain uninterpreted.
        </p>
      ) : unavailable ? (
        <p className="tl-unavailable">
          This channel is unavailable for the selected observations.
        </p>
      ) : (
        <div
          ref={wrapper}
          className="tl-canvas-wrap"
          onPointerDown={(e) => {
            if (brush) {
              dragStart.current = distance(e.clientX);
              e.currentTarget.setPointerCapture(e.pointerId);
            } else onCursor(distance(e.clientX));
          }}
          onPointerMove={(e) => {
            const d = distance(e.clientX);
            if (raf.current !== null) cancelAnimationFrame(raf.current);
            raf.current = requestAnimationFrame(() => {
              onCursor(d);
              raf.current = null;
            });
          }}
          onPointerUp={(e) => {
            if (brush && dragStart.current !== null) {
              const end = distance(e.clientX),
                start = dragStart.current;
              if (Math.abs(end - start) >= 10)
                onZoom([Math.min(start, end), Math.max(start, end)]);
              dragStart.current = null;
            }
          }}
          onPointerCancel={() => {
            dragStart.current = null;
          }}
        >
          <canvas
            ref={canvas}
            role="img"
            aria-label={`${info.title} versus distance; use the shared cursor range below for keyboard access, or the data table alternative.`}
          />
          <div
            className="tl-crosshair"
            style={{
              left: `calc(52px + (100% - 72px) * ${Math.max(0, Math.min(1, (cursor - zoom[0]) / (zoom[1] - zoom[0])))})`,
            }}
          />
        </div>
      )}
    </section>
  );
}
export function TelemetryCharts(props: Props) {
  const channels: Channel[] =
    props.view === "inputs"
      ? ["throttle_percent", "brake", "gear", "drs_raw"]
      : props.view === "overview"
        ? ["delta", "speed_kmh"]
        : ["delta", "speed_kmh", "rpm"];
  return (
    <div className="tl-chart-stack">
      {channels.map((channel) => (
        <ChannelChart key={channel} {...props} channel={channel} />
      ))}
      <ChartDataTable
        comparison={props.comparison}
        zoom={props.zoom}
        channels={channels}
      />
    </div>
  );
}
function ChartDataTable({
  comparison: c,
  zoom,
  channels,
}: {
  comparison: Comparison;
  zoom: [number, number];
  channels: Channel[];
}) {
  const [page, setPage] = useState(0);
  const start = nearestIndex(c.distance_m, zoom[0]),
    end = nearestIndex(c.distance_m, zoom[1]);
  const count = end - start + 1,
    safePage = Math.min(page, Math.floor((count - 1) / 100));
  const rows = c.distance_m.slice(
    start + safePage * 100,
    Math.min(end + 1, start + safePage * 100 + 100),
  );
  return (
    <details className="tl-data-table">
      <summary>Accessible data table · aligned DERIVED samples</summary>
      <div className="tl-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Distance m</th>
              {channels.map((ch) => (
                <th key={ch}>
                  {CHANNELS[ch].title}
                  {ch === "delta" ? "" : " · A / B"}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((d, j) => {
              const i = start + safePage * 100 + j;
              return (
                <tr key={d}>
                  <th>{d.toFixed(1)}</th>
                  {channels.map((ch) => (
                    <td key={ch}>
                      {ch === "delta"
                        ? signed(c.valid_mask[i] ? c.delta_seconds[i] : null)
                        : [c.a, c.b]
                            .map((s) =>
                              numberLabel(
                                s.channel_valid_masks[ch]?.[i]
                                  ? s.channels[ch]?.[i]
                                  : null,
                              ),
                            )
                            .join(" / ")}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="tl-table-pagination">
        <button disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
          Previous samples
        </button>
        <span>
          {safePage * 100 + 1}–{Math.min(count, (safePage + 1) * 100)} of{" "}
          {count}
        </span>
        <button
          disabled={(safePage + 1) * 100 >= count}
          onClick={() => setPage(safePage + 1)}
        >
          Next samples
        </button>
      </div>
    </details>
  );
}
