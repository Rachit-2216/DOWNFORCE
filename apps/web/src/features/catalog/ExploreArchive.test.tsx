import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ExploreArchive } from "./ExploreArchive";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

it("offers real shared race workspaces before archive filters without nesting main landmarks", async () => {
  const sessionId =
    "session-2024-event-formula-1-hungarian-grand-prix-2024-sha256-cdc7f18370f678e135dd423e4794e6c76b2a318c08c5b7cf80f158e32ed61f10-type-race";
  vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
    const url = String(input);
    const body = url.includes("catalog/seasons")
      ? {
          items: [{ year: 2024, event_count: 24, completed_event_count: 24 }],
          total: 27,
          event_count: 526,
          completed_event_count: 515,
          latest_completed_event_date: "2026-08-23",
        }
      : url.includes("catalog/events")
        ? { items: [], total: 0, offset: 0, limit: 100 }
        : {
            items: [
              {
                session_id: sessionId,
                event_name: "Hungarian Grand Prix",
                season: 2024,
                session_type: "race",
                provider: "fastf1",
              },
            ],
            total: 1,
            offset: 0,
            limit: 100,
          };
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  render(<ExploreArchive />);
  const tools = await screen.findByRole("region", {
    name: "Choose a race. Make your next move.",
  });
  const race = await within(tools).findByRole("heading", {
    name: "Hungarian Grand Prix",
  });
  expect(
    within(race.closest("article")!).getByRole("link", { name: /strategy/i }),
  ).toHaveAttribute(
    "href",
    `/app/replay/${sessionId}?mode=strategy&driver=1&lap=5`,
  );
  const filters = screen.getByRole("region", { name: "Archive filters" });
  expect(
    tools.compareDocumentPosition(filters) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
  expect(
    screen.getByRole("link", { name: "Browse the archive" }),
  ).toHaveAttribute("href", "#archive-filters");
  expect(screen.getAllByRole("main")).toHaveLength(1);
  expect(screen.getByText("515")).toBeInTheDocument();
  expect(screen.getByText("27")).toBeInTheDocument();
  expect(screen.getByText("2026-08-23")).toBeInTheDocument();
});
