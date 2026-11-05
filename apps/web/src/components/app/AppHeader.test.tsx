import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AppHeader } from "./AppHeader";
vi.mock("next/navigation", () => ({ usePathname: () => "/app/replay/race-1" }));
afterEach(cleanup);
it("preserves race context in native tool links and supports same-workspace switching", () => {
  const choose = vi.fn();
  render(
    <AppHeader
      context="Hungary / lap 5"
      activeSection="replay"
      raceContext={{ sessionId: "race-1", driverId: "driver-44", timeMs: 5000 }}
      onToolChange={choose}
    />,
  );
  const nav = screen.getByRole("navigation", { name: "Product navigation" });
  const strategy = within(nav).getByRole("link", { name: /Strategy/ });
  expect(strategy).toHaveAttribute(
    "href",
    "/app/replay/race-1?mode=strategy&driver=driver-44&t=5000",
  );
  fireEvent.click(strategy);
  expect(choose).toHaveBeenCalledWith("strategy");
  expect(nav.querySelectorAll('[aria-current="page"]')).toHaveLength(1);
});
