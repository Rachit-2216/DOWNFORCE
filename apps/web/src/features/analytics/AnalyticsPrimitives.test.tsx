import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  BarChart,
  CoverageNote,
  LineChart,
  formatMetric,
} from "./AnalyticsPrimitives";

describe("analytics primitives", () => {
  it("renders explicit coverage and unavailable values", () => {
    render(
      <CoverageNote
        coverage={{
          sample_count: 12_692,
          race_count: 321,
          eligible_race_count: 515,
          missing_count: 194,
          verified_count: 319,
          good_count: 2,
          quality_exclusions: 0,
          analytics_version: "1.1.0",
          archive_source_revision: "fixture",
          ratio: 321 / 515,
        }}
      />,
    );
    expect(screen.getByText(/12,692 samples across 321 of 515/)).toBeVisible();
    expect(screen.getByText(/194 races unavailable/)).toBeVisible();
    expect(formatMetric(null)).toBe("Unavailable");
    expect(formatMetric(6.482927)).toBe("6.5");
  });

  it("gives charts accessible text and printed values", () => {
    const { rerender } = render(
      <BarChart
        label="Driver wins"
        rows={[{ label: "Driver A", value: 10 }]}
      />,
    );
    expect(screen.getByLabelText("Driver wins")).toBeVisible();
    expect(screen.getByText("10")).toBeVisible();

    rerender(
      <LineChart
        label="Recorded points by round"
        series={[
          {
            name: "Driver A",
            points: [
              { round_number: 1, value: 10 },
              { round_number: 2, value: 16 },
            ],
          },
        ]}
      />,
    );
    expect(
      screen.getByRole("img", { name: "Recorded points by round" }),
    ).toBeVisible();
    expect(screen.getByText("Series 1 · Driver A")).toBeVisible();
    expect(screen.getByText(/2 samples.*latest 16 at round 2/)).toBeVisible();

    rerender(
      <LineChart
        label="Relative recorded position"
        xAxisLabel="lap"
        valuePrefix="P"
        series={[
          {
            name: "Driver A",
            points: [
              { round_number: 1, value: 18, display_value: 3 },
              { round_number: 2, value: 20, display_value: 1 },
            ],
          },
        ]}
      />,
    );
    expect(screen.getByText(/2 samples.*latest P1 at lap 2/)).toBeVisible();
  });
});
