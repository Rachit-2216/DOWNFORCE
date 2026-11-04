import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { TelemetryTrack } from "./TelemetryTrack";
import type { Comparison, Microsectors } from "./types";
afterEach(cleanup);
it("keeps the API's one-based derived microsector numbering", () => {
  const side = {
    channels: { x: [0, 10], y: [0, 20] },
    channel_valid_masks: { x: [true, true], y: [true, true] },
  };
  const comparison = {
    distance_m: [0, 100],
    a: side,
    b: side,
  } as unknown as Comparison;
  const microsectors = {
    sectors: Array.from({ length: 20 }, (_, i) => ({
      index: i + 1,
      start_m: i * 5,
      end_m: (i + 1) * 5,
      label: "Test fixture derived bin",
      delta_seconds: null,
    })),
  } as Microsectors;
  render(
    <TelemetryTrack
      comparison={comparison}
      corners={null}
      microsectors={microsectors}
      cursor={0}
      onCursor={vi.fn()}
      onZoom={vi.fn()}
    />,
  );
  expect(screen.getByText("1", { selector: "span" })).toBeInTheDocument();
  expect(screen.getByText("20", { selector: "span" })).toBeInTheDocument();
  expect(
    screen.queryByText("21", { selector: "span" }),
  ).not.toBeInTheDocument();
});
