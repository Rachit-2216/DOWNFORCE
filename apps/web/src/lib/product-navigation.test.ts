import { describe, expect, it } from "vitest";

import { detailedRaceHref, replayLibraryHref } from "./product-navigation";

describe("online product navigation", () => {
  it("carries the current driver and exact cursor into another race tool", () => {
    expect(
      detailedRaceHref("race-1", "strategy", {
        driverId: "driver /44",
        timeMs: 5123.6,
      }),
    ).toBe("/app/replay/race-1?mode=strategy&driver=driver+%2F44&t=5124");
  });
  it.each(["replay", "intelligence", "strategy"] as const)(
    "opens %s in the existing race workspace without inventing driver context",
    (mode) => {
      expect(detailedRaceHref("race/id ?&", mode)).toBe(
        `/app/replay/race%2Fid%20%3F%26?mode=${mode}`,
      );
    },
  );

  it("keeps the full replay library reachable with optional mode intent", () => {
    expect(replayLibraryHref()).toBe("/app/replays");
    expect(replayLibraryHref("intelligence")).toBe(
      "/app/replays?mode=intelligence",
    );
    expect(replayLibraryHref("strategy")).toBe("/app/replays?mode=strategy");
  });
});
