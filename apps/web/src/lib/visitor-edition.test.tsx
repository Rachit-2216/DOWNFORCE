import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AppHeader } from "@/components/app/AppHeader";
import { ExploreArchive } from "@/features/catalog/ExploreArchive";
import TelemetryPage from "@/app/app/telemetry/page";

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe("visitor edition", () => {
  it("keeps local telemetry navigation by default", () => {
    vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", "local");
    render(<AppHeader context="Explore" />);
    expect(screen.getByRole("link", { name: "Telemetry" })).toHaveAttribute(
      "href",
      "/app/telemetry",
    );
  });

  it("hides telemetry navigation, but keeps useful visitor areas", () => {
    vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", "visitor");
    render(<AppHeader context="Explore" />);
    expect(
      screen.queryByRole("link", { name: "Telemetry" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Analytics" })).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Detailed replays" }),
    ).toBeInTheDocument();
  });

  it.each(["visitor", "local"])(
    "offers honest %s coverage without removing track, weather or ML",
    (profile) => {
      vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", profile);
      vi.spyOn(globalThis, "fetch").mockImplementation(
        () => new Promise(() => {}),
      );
      render(<ExploreArchive />);
      expect(
        screen.getByRole("option", { name: "Track position" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("option", { name: "Weather" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("option", { name: "ML intelligence" }),
      ).toBeInTheDocument();
      if (profile === "visitor") {
        expect(
          screen.queryByRole("option", { name: "Telemetry" }),
        ).not.toBeInTheDocument();
        expect(
          screen.getByText(/curated visitor edition/i),
        ).toBeInTheDocument();
        expect(
          screen.getByRole("link", { name: "Run locally" }),
        ).toHaveAttribute(
          "href",
          "https://github.com/Rachit-2216/DOWNFORCE#run-locally",
        );
      } else {
        expect(
          screen.getByRole("option", { name: "Telemetry" }),
        ).toBeInTheDocument();
        expect(
          screen.queryByText(/curated visitor edition/i),
        ).not.toBeInTheDocument();
      }
    },
  );

  it("renders a useful local research guide at direct visitor telemetry URLs without fetching", () => {
    vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", "visitor");
    const fetcher = vi.spyOn(globalThis, "fetch");
    render(<TelemetryPage />);
    expect(
      screen.getByRole("heading", { name: "Telemetry research runs locally" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run locally" })).toHaveAttribute(
      "href",
      "https://github.com/Rachit-2216/DOWNFORCE#run-locally",
    );
    expect(
      screen.getByText(/provider availability and terms/i),
    ).toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
  });
});
