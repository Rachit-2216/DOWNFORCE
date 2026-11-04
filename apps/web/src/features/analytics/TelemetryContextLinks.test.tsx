import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as client from "@/lib/api/client";
import type { AnalyticsCoverage } from "@/lib/api/types";
import { RaceAnalytics } from "./RaceAnalytics";
import { EntityProfile } from "./EntityProfile";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});
const coverage: AnalyticsCoverage = {
  sample_count: 0,
  race_count: 0,
  eligible_race_count: 0,
  missing_count: 0,
  verified_count: 0,
  good_count: 0,
  quality_exclusions: 0,
  ratio: 0,
  analytics_version: "test",
  archive_source_revision: "test",
};
const raceId = "archive-2024-round-08-race";
it.each(["local", "visitor"])(
  "uses %s race analytics telemetry availability",
  async (profile) => {
    vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", profile);
    vi.spyOn(client, "getRaceAnalytics").mockResolvedValue({
      analytics_version: "test",
      archive_source_revision: "test",
      event: { season: 2024, round_number: 8, event_name: "Monaco" },
      summary: {},
      drivers: [],
      biggest_movers: [],
      position_progression: [],
      coverage: { results: coverage, laps: coverage, pits: coverage },
    });
    render(<RaceAnalytics sessionId={raceId} />);
    await screen.findByRole("heading", { name: "Monaco" });
    if (profile === "visitor") {
      expect(
        screen.queryByRole("link", { name: "Open Telemetry Lab" }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: "Open race overview" }),
      ).toBeInTheDocument();
      return;
    }
    expect(
      await screen.findByRole("link", { name: "Open Telemetry Lab" }),
    ).toHaveAttribute("href", `/app/telemetry?race_session_id=${raceId}`);
  },
);
it.each(["local", "visitor"])(
  "uses %s entity telemetry availability",
  async (profile) => {
    vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", profile);
    vi.spyOn(client, "getEntityAnalytics").mockResolvedValue({
      analytics_version: "test",
      archive_source_revision: "test",
      entity: { driver_name: "Driver" },
      summary: {},
      races: {
        items: [
          {
            session_id: raceId,
            season: 2024,
            event_name: "Monaco",
            round_number: 8,
          },
        ],
        total: 1,
        offset: 0,
        limit: 50,
      },
      coverage: { results: coverage },
    });
    render(<EntityProfile kind="drivers" id="driver" />);
    await screen.findByRole("link", { name: /2024.*Monaco/ });
    if (profile === "visitor") {
      expect(
        screen.queryByRole("link", { name: "Telemetry Lab · Monaco" }),
      ).not.toBeInTheDocument();
      return;
    }
    expect(
      await screen.findByRole("link", { name: "Telemetry Lab · Monaco" }),
    ).toHaveAttribute("href", `/app/telemetry?race_session_id=${raceId}`);
  },
);
