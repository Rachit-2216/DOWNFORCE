import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import LandingPage from "./page";

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({ items: [], offset: 0, limit: 100, total: 0 }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    ),
  );
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("landing page", () => {
  it("places shared online race entry points between evidence and product features", () => {
    const { container } = render(<LandingPage />);
    const tools = screen.getByRole("region", {
      name: "Choose a race. Make your next move.",
    });
    expect(tools.previousElementSibling).toHaveAttribute(
      "aria-label",
      "How to read the evidence",
    );
    expect(tools.nextElementSibling).toHaveClass("public-feature-grid");
    expect(container.querySelector(".public-products")).toContainElement(tools);
  });

  it("puts real online product destinations first", () => {
    render(<LandingPage />);

    expect(
      screen.getByRole("heading", {
        name: "DOWNFORCE",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /try a race/i })).toHaveAttribute(
      "href",
      "/app/replays",
    );
    expect(
      screen.getByRole("link", { name: /explore history/i }),
    ).toHaveAttribute("href", "/app");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    for (const [name, href] of [
      ["Explore", "/app"],
      ["Analytics", "/app/analytics"],
      ["Replays", "/app/replays"],
      ["Anatomy", "#anatomy"],
    ]) {
      expect(within(nav).getByRole("link", { name })).toHaveAttribute(
        "href",
        href,
      );
    }
    expect(screen.getByText(/27 seasons/i)).toBeInTheDocument();
    expect(screen.getByText(/515 races/i)).toBeInTheDocument();
    expect(screen.getByText(/13 detailed replays/i)).toBeInTheDocument();
  });

  it("uses one compact anatomy region followed by actionable product features", () => {
    const { container } = render(<LandingPage />);
    const anatomy = screen.getByRole("region", {
      name: "One machine. Eight connected systems.",
    });
    expect(
      within(anatomy).getByRole("button", { name: "Front Wing" }),
    ).toBeInTheDocument();
    expect(
      container.querySelectorAll(".component-spotlight, .story-section"),
    ).toHaveLength(0);
    expect(
      screen.getByRole("heading", {
        name: /see only what was knowable then/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: /compare the decision\. not the hindsight/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /open analytics/i }),
    ).toHaveAttribute("href", "/app/analytics");
    expect(
      screen.getByRole("link", { name: /choose a replay/i }),
    ).toHaveAttribute("href", "/app/replays");
    expect(
      screen.queryByText(/final legal wording|local clone/i),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /data attribution/i }),
    ).toHaveAttribute(
      "href",
      "https://github.com/Rachit-2216/DOWNFORCE#data-accuracy-and-attribution",
    );
  });
});
