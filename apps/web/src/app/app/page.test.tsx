import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ArchiveWorkspace } from "@/features/catalog/ArchiveWorkspace";
import { RaceOverview } from "@/features/catalog/RaceOverview";

import DownforceAppPage from "./page";

const seasons = {
  items: [
    {
      year: 2024,
      event_count: 24,
      completed_event_count: 24,
      earliest_event_date: "2024-03-02",
      latest_event_date: "2024-12-08",
    },
  ],
  total: 1,
  event_count: 526,
  completed_event_count: 515,
  latest_completed_event_date: "2026-08-23",
};

function events(name: string, eventId: string) {
  return {
    items: [
      {
        event_id: eventId,
        season: 2024,
        round_number: 12,
        name,
        event_date: "2024-07-07",
        circuit_name: "Silverstone Circuit",
        country: "United Kingdom",
        locality: "Silverstone",
        status: "completed",
        sessions: [
          {
            session_id: `archive-${eventId}`,
            event_id: eventId,
            session_type: "race",
            status: "completed",
            capability_tier: "full_downforce",
            capabilities: { results: true },
            quality: { status: "verified", reasons: [], checks: {} },
            provenance: [],
            row_counts: { results: 20 },
            legacy_session_id: "session-2024-event-test-type-race",
          },
        ],
      },
    ],
    offset: 0,
    limit: 100,
    total: 1,
  };
}

function response(value: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
}

describe("application shell", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    window.history.replaceState(null, "", "/");
  });

  it("loads the historical archive into Explore", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      String(input).includes("/catalog/seasons")
        ? response(seasons)
        : response(events("British Grand Prix", "event-2024-round-12")),
    );

    render(<DownforceAppPage />);
    expect(
      screen.getByRole("heading", {
        name: /every race.*only the data that actually exists/i,
      }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: /british grand prix/i }),
    ).toHaveAttribute("href", "/app/events/event-2024-round-12");
    expect(screen.getByRole("link", { name: "DOWNFORCE" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(
      screen.getByRole("link", { name: "Skip to main content" }),
    ).toHaveAttribute("href", "#main-content");
  });

  it("persists the explicit All seasons selection across remounts", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) =>
      String(input).includes("/catalog/seasons")
        ? response(seasons)
        : response(events("British Grand Prix", "event-2024-round-12")),
    );

    const view = render(<DownforceAppPage />);
    await screen.findByRole("link", { name: /british grand prix/i });
    fireEvent.click(screen.getByRole("button", { name: /all.*26\+ seasons/i }));
    await waitFor(() => expect(window.location.search).toBe("?season=all"));
    expect(
      screen.getByRole("heading", { name: "All seasons" }),
    ).toBeInTheDocument();

    view.unmount();
    render(<DownforceAppPage />);
    expect(
      await screen.findByRole("heading", { name: "All seasons" }),
    ).toBeInTheDocument();
  });

  it("loads subsequent catalog pages without hiding the total", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.includes("/catalog/seasons")) return response(seasons);
      if (url.includes("offset=1"))
        return response({
          ...events("Italian Grand Prix", "event-2024-round-16"),
          offset: 1,
          total: 2,
        });
      return response({
        ...events("British Grand Prix", "event-2024-round-12"),
        total: 2,
      });
    });

    render(<DownforceAppPage />);
    await screen.findByRole("link", { name: /british grand prix/i });
    fireEvent.click(
      await screen.findByRole("button", { name: "Load more races" }),
    );
    expect(
      await screen.findByRole("link", { name: /italian grand prix/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/showing/i)).toHaveTextContent(
      "Showing 2 of 2 matches",
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "2 matches / 2 full DOWNFORCE loaded",
    );
    expect(screen.getByRole("list").closest("[aria-live]")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Load more races" }),
    ).not.toBeInTheDocument();
  });

  it("keeps skip-link targets available in catalog failure gates", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("offline"));

    const race = render(<RaceOverview eventId="event-2024-round-12" />);
    expect(await screen.findByRole("alert")).toHaveAttribute(
      "id",
      "main-content",
    );

    race.unmount();
    render(<ArchiveWorkspace eventId="event-2024-round-12" />);
    expect(await screen.findByRole("alert")).toHaveAttribute(
      "id",
      "main-content",
    );
  });

  it("retries the archive after a network failure", async () => {
    let offline = true;
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (offline) {
        offline = false;
        return Promise.reject(new Error("offline"));
      }
      return String(input).includes("/catalog/seasons")
        ? response(seasons)
        : response(events("Recovered Grand Prix", "event-recovered"));
    });

    render(<DownforceAppPage />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Retry archive" }),
    );
    expect(
      await screen.findByRole("link", { name: /recovered grand prix/i }),
    ).toBeInTheDocument();
  });
});
