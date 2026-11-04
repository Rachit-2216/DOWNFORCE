"use client";
import { useState } from "react";
import { numberLabel, signed } from "./telemetry-utils";
import type { TelemetryBundle, Trace } from "./types";
export function LapContext({ trace, side }: { trace: Trace; side: "A" | "B" }) {
  const l = trace.context;
  return (
    <article className={`tl-lap-context tl-side-${side.toLowerCase()}`}>
      <header>
        <span className="tl-badge">
          {side} · DRIVER {trace.driver}
        </span>
        <h3>LAP {trace.lap_number}</h3>
        <strong>{numberLabel(l.lap_time_seconds, 3)} s</strong>
      </header>
      <dl>
        <div>
          <dt>Compound / stint</dt>
          <dd>
            {l.compound ?? "Not recorded"} / {l.stint ?? "—"}
          </dd>
        </div>
        <div>
          <dt>Tyre life</dt>
          <dd>{l.tyre_life ?? "—"} laps</dd>
        </div>
        <div>
          <dt>Observed sectors</dt>
          <dd>
            {[l.sector_1_seconds, l.sector_2_seconds, l.sector_3_seconds]
              .map((v) => numberLabel(v, 3))
              .join(" / ")}{" "}
            s
          </dd>
        </div>
        <div>
          <dt>Weather</dt>
          <dd>
            {l.weather_available
              ? "Recorded · values not exposed by this product"
              : "Not recorded"}
          </dd>
        </div>
        <div>
          <dt>Phase / track status</dt>
          <dd>
            {l.qualifying_phase ?? "—"} / {l.track_status ?? "—"}
          </dd>
        </div>
      </dl>
      <p className="tl-caption">
        {[
          l.reliable ? "Reliable" : "Unreliable",
          l.deleted ? "Deleted" : "Not deleted",
          l.provider_generated ? "Provider generated" : "Observed lap",
          l.pit_in ? "Pit in" : null,
          l.pit_out ? "Pit out" : null,
          ...trace.flags,
        ]
          .filter(Boolean)
          .join(" · ")}
      </p>
    </article>
  );
}
export function TelemetryDetails({ bundle }: { bundle: TelemetryBundle }) {
  const c = bundle.comparison;
  return (
    <div className="tl-details">
      <section className="tl-panel">
        <p className="tl-eyebrow">PROVENANCE & VALIDATION</p>
        <h2>Know what you are comparing</h2>
        <dl>
          <div>
            <dt>Canonical revision</dt>
            <dd className="tl-revision">{c.revision}</dd>
          </div>
          <div>
            <dt>Algorithm / corner version</dt>
            <dd>
              {c.algorithm_version} / {c.corner_version}
            </dd>
          </div>
          <div>
            <dt>Delta convention</dt>
            <dd>{c.delta_convention}</dd>
          </div>
          <div>
            <dt>Physical-overlap end delta</dt>
            <dd>{signed(c.end_delta_seconds)}</dd>
          </div>
          <div>
            <dt>Observed lap-time difference</dt>
            <dd>{signed(c.observed_lap_delta_seconds)}</dd>
          </div>
          <div>
            <dt>Finish-line comparison</dt>
            <dd>
              {c.finish_line_agreement_applicable
                ? `Applicable · error ${numberLabel(c.finish_delta_error_seconds, 3)} s`
                : "Not applicable — cropped physical overlap is not full-lap finish validation"}
            </dd>
          </div>
          <div>
            <dt>Coverage A / B</dt>
            <dd>
              {[bundle.traceA, bundle.traceB]
                .map(
                  (t) =>
                    `Start ${t.start_covered ? "covered" : "cropped"}; finish ${t.finish_covered ? "covered" : "cropped"}`,
                )
                .join(" / ")}
            </dd>
          </div>
          <div>
            <dt>Quality flags</dt>
            <dd>{c.flags.join(" · ") || "No flags reported"}</dd>
          </div>
        </dl>
        <p className="tl-caption">
          Aligned distance, elapsed time, interpolation, deltas and window
          metrics are DERIVED. Native source clocks and sensor values below
          retain PROVIDER_NATIVE provenance. No finish-agreement claim is made
          from overlap-only comparisons.
        </p>
      </section>
      <div className="tl-context-grid">
        <LapContext trace={bundle.traceA} side="A" />
        <LapContext trace={bundle.traceB} side="B" />
      </div>
      <section className="tl-panel">
        <h2>Channel availability</h2>
        <div className="tl-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Channel</th>
                <th>A</th>
                <th>B</th>
              </tr>
            </thead>
            <tbody>
              {[
                ...new Set([
                  ...Object.keys(c.availability_a),
                  ...Object.keys(c.availability_b),
                ]),
              ].map((ch) => (
                <tr key={ch}>
                  <th>{ch}</th>
                  <td>{c.availability_a[ch] ?? "Not recorded"}</td>
                  <td>{c.availability_b[ch] ?? "Not recorded"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <NativeTable trace={bundle.traceA} side="A" />
      <NativeTable trace={bundle.traceB} side="B" />
    </div>
  );
}
function NativeTable({ trace, side }: { trace: Trace; side: string }) {
  const [page, setPage] = useState(0),
    raw = trace.source_samples,
    keys = Object.keys(raw.channels),
    total = raw.session_time_ns.length;
  return (
    <details className="tl-data-table">
      <summary>
        {side} · RAW PROVIDER_NATIVE observations · {total.toLocaleString()}{" "}
        samples
      </summary>
      <p>
        Native sensor clocks are not a shared comparison grid. DRS raw codes are
        unknown; parser defaults are not observations.
      </p>
      <div className="tl-table-wrap">
        <table>
          <thead>
            <tr>
              <th>Session clock ns</th>
              <th>Source</th>
              {keys.map((k) => (
                <th key={k}>{k === "brake" ? "BRAKE ACTIVE" : k}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {raw.session_time_ns
              .slice(page * 100, (page + 1) * 100)
              .map((t, j) => {
                const i = page * 100 + j;
                return (
                  <tr key={i}>
                    <th>{t}</th>
                    <td>{raw.source_sensors[i] ?? "Unknown"}</td>
                    {keys.map((k) => (
                      <td key={k}>
                        {raw.channel_valid_masks[k]?.[i]
                          ? numberLabel(raw.channels[k][i])
                          : "—"}
                      </td>
                    ))}
                  </tr>
                );
              })}
          </tbody>
        </table>
      </div>
      <div className="tl-table-pagination">
        <button disabled={!page} onClick={() => setPage((p) => p - 1)}>
          Previous native samples
        </button>
        <span>
          {page * 100 + 1}–{Math.min(total, (page + 1) * 100)} / {total}
        </span>
        <button
          disabled={(page + 1) * 100 >= total}
          onClick={() => setPage((p) => p + 1)}
        >
          Next native samples
        </button>
      </div>
    </details>
  );
}
