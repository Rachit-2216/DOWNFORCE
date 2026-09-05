import Link from "next/link";
import type { ReactNode } from "react";

import type { AnalyticsCoverage, JsonValue } from "@/lib/api/types";

export const analyticsNav = [
  ["Seasons", "/app/analytics"],
  ["Drivers", "/app/analytics/drivers"],
  ["Constructors", "/app/analytics/constructors"],
  ["Circuits", "/app/analytics/circuits"],
  ["Compare", "/app/analytics/compare"],
  ["Rankings", "/app/analytics/rankings"],
] as const;

export function AnalyticsNav() {
  return (
    <nav className="analytics-nav" aria-label="Analytics sections">
      {analyticsNav.map(([label, href]) => (
        <Link key={href} href={href}>
          {label}
        </Link>
      ))}
    </nav>
  );
}

export function AnalyticsPage({ children }: { children: ReactNode }) {
  return (
    <main className="analytics-shell" id="main-content">
      <AnalyticsNav />
      {children}
    </main>
  );
}

export function PageIntro({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="analytics-intro">
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      {children ? (
        <div className="analytics-intro__detail">{children}</div>
      ) : null}
    </header>
  );
}

export function MetricStrip({
  metrics,
}: {
  metrics: { label: string; value: JsonValue | undefined }[];
}) {
  return (
    <dl className="metric-strip">
      {metrics.map(({ label, value }) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{formatMetric(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function CoverageNote({ coverage }: { coverage?: AnalyticsCoverage }) {
  if (!coverage) return null;
  return (
    <p className="coverage-note">
      {coverage.sample_count.toLocaleString()} samples across{" "}
      {coverage.race_count} of {coverage.eligible_race_count} eligible races ·{" "}
      {coverage.verified_count} verified
      {coverage.good_count ? ` · ${coverage.good_count} good` : ""}
      {coverage.missing_count
        ? ` · ${coverage.missing_count} races unavailable`
        : ""}
      {coverage.quality_exclusions
        ? ` · ${coverage.quality_exclusions} quality exclusions`
        : ""}
    </p>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="analytics-empty">{children}</p>;
}

export function formatMetric(value: JsonValue | undefined): string {
  if (value === null || value === undefined) return "Unavailable";
  if (typeof value === "number") {
    return Number.isInteger(value)
      ? value.toLocaleString()
      : value.toLocaleString(undefined, { maximumFractionDigits: 1 });
  }
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return value.replaceAll("_", " ");
  return "—";
}

export function BarChart({
  rows,
  label,
}: {
  rows: { label: string; value: number }[];
  label: string;
}) {
  const max = Math.max(1, ...rows.map((row) => row.value));
  return (
    <figure className="bar-chart" aria-label={label}>
      {rows.map((row) => (
        <div key={row.label} className="bar-chart__row">
          <span>{row.label}</span>
          <i style={{ width: `${Math.max(1, (row.value / max) * 100)}%` }} />
          <strong>{formatMetric(row.value)}</strong>
        </div>
      ))}
      <figcaption>
        {label}. Values are also printed beside every bar.
      </figcaption>
    </figure>
  );
}

export function LineChart({
  series,
  label,
  xAxisLabel = "round",
  valuePrefix = "",
}: {
  series: {
    name: string;
    points: {
      round_number: number;
      value: number;
      display_value?: number;
    }[];
  }[];
  label: string;
  xAxisLabel?: string;
  valuePrefix?: string;
}) {
  const values = series.flatMap((item) =>
    item.points.map((point) => point.value),
  );
  const rounds = series.flatMap((item) =>
    item.points.map((point) => point.round_number),
  );
  if (!values.length || !rounds.length)
    return <EmptyState>No trend samples are available.</EmptyState>;
  const max = Math.max(...values, 1);
  const maxRound = Math.max(...rounds, 1);
  const colors = ["#246bff", "#f44336", "#ffd400", "#f4f7ff", "#71809d"];
  const patterns = ["none", "18 8", "4 7", "14 5 3 5", "2 5"];
  return (
    <figure className="line-chart">
      <svg viewBox="0 0 900 300" role="img" aria-label={label}>
        {[0, 1, 2, 3].map((line) => (
          <line
            key={line}
            x1="30"
            y1={40 + line * 70}
            x2="870"
            y2={40 + line * 70}
          />
        ))}
        {series.map((item, index) => {
          const points = item.points
            .map(
              (point) =>
                `${30 + (point.round_number / maxRound) * 840},${260 - (point.value / max) * 220}`,
            )
            .join(" ");
          return (
            <polyline
              key={item.name}
              points={points}
              style={{
                stroke: colors[index % colors.length],
                strokeDasharray: patterns[index % patterns.length],
              }}
            />
          );
        })}
      </svg>
      <figcaption>
        <span>{label}</span>
        <ol>
          {series.map((item, index) => {
            const latest = item.points.at(-1);
            return (
              <li key={item.name}>
                <i
                  aria-hidden="true"
                  style={{
                    borderColor: colors[index % colors.length],
                    borderStyle:
                      index % patterns.length === 0 ? "solid" : "dashed",
                  }}
                />
                <strong>
                  Series {index + 1} · {item.name}
                </strong>
                <small>
                  {item.points.length} samples
                  {latest
                    ? ` · latest ${valuePrefix}${formatMetric(latest.display_value ?? latest.value)} at ${xAxisLabel} ${formatMetric(latest.round_number)}`
                    : ""}
                </small>
              </li>
            );
          })}
        </ol>
      </figcaption>
    </figure>
  );
}
