import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as client from "@/lib/api/client";
import { RaceOverview } from "./RaceOverview";
import { ArchiveWorkspace } from "./ArchiveWorkspace";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});
it("carries the selected car number and lap from archive evidence into its verified detailed session", async () => {
  window.history.replaceState(null, "", "/?mode=intelligence&lap=2");
  vi.spyOn(globalThis, "fetch").mockImplementation(
    async (input) =>
      new Response(
        JSON.stringify(
          String(input).includes("/results")
            ? {
                items: [
                  {
                    driver_id: "archive-driver",
                    driver_name: "Driver",
                    car_number: 81,
                    laps_completed: 5,
                  },
                ],
              }
            : {
                event_id: "event",
                name: "Race",
                season: 2024,
                round_number: 13,
                sessions: [
                  {
                    session_id: "archive-race",
                    legacy_session_id: "canonical-race",
                    status: "completed",
                    capability_tier: "full_downforce",
                    quality: { status: "verified" },
                    row_counts: { laps: 5 },
                    provenance: { provider: "fixture" },
                    capabilities: {
                      results: true,
                      ml_intelligence: true,
                      strategy_simulation: true,
                    },
                  },
                ],
              },
        ),
        { status: 200 },
      ),
  );
  render(<ArchiveWorkspace eventId="event" />);
  const link = await screen.findByRole("link", {
    name: /Continue with verified detailed data/,
  });
  const url = new URL(link.getAttribute("href")!, "http://localhost");
  expect(url.pathname).toBe("/app/replay/canonical-race");
  expect(url.searchParams.get("driver")).toBe("81");
  expect(url.searchParams.get("lap")).toBe("2");
  expect(url.searchParams.get("mode")).toBe("intelligence");
});
it.each([RaceOverview, ArchiveWorkspace])(
  "keeps a retry and archive exit when a race request fails",
  async (Component) => {
    vi.spyOn(client, "getCatalogEvent").mockRejectedValue(new Error("Offline"));
    render(<Component eventId="event" />);
    await screen.findByRole("alert");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("link", { name: "Browse races" }),
    ).toHaveAttribute("href", "/app");
    expect(client.getCatalogEvent).toHaveBeenCalledTimes(2);
  },
);
