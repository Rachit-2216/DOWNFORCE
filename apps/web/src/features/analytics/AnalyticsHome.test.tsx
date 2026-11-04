import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AnalyticsHome } from "./AnalyticsHome";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("recovers a failed season register instead of loading forever", async () => {
  vi.spyOn(globalThis, "fetch")
    .mockRejectedValueOnce(new Error("offline"))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          items: [{ year: 2024, event_count: 24, completed_event_count: 24 }],
          total: 1,
          event_count: 24,
          completed_event_count: 24,
          latest_completed_event_date: "2024-12-08",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
  render(<AnalyticsHome />);
  fireEvent.click(await screen.findByRole("button", { name: "Retry seasons" }));
  expect(
    (await screen.findByText("24 completed races")).closest("a"),
  ).toHaveAttribute("href", "/app/analytics/seasons/2024");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
