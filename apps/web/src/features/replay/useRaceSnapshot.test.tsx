import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as api from "@/lib/api/client";
import type { RaceState } from "@/lib/api/types";
import { useRaceSnapshot } from "./useRaceSnapshot";
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});
it("lets a slow request finish during playback and then requests the latest cursor", async () => {
  vi.useFakeTimers();
  let finish!: (value: RaceState) => void;
  const fetcher = vi
    .spyOn(api, "getRaceState")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockImplementation(
      async (_id, cursor) =>
        ({
          session_time_ms: "timeMs" in cursor ? cursor.timeMs : 0,
          drivers: [],
        }) as unknown as RaceState,
    );
  const hook = renderHook(
    ({ time }) => useRaceSnapshot("race", time, true, 0),
    { initialProps: { time: 1000 } },
  );
  hook.rerender({ time: 1500 });
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][2]?.signal?.aborted).toBe(false);
  await act(async () =>
    finish({ session_time_ms: 1000, drivers: [] } as unknown as RaceState),
  );
  expect(hook.result.current.data?.session_time_ms).toBe(1000);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(250);
  });
  expect(fetcher.mock.calls[1][1]).toEqual({ timeMs: 1500 });
});
it("aborts playback polling and hides its late response after a paused backwards seek", async () => {
  let finish!: (value: RaceState) => void;
  vi.spyOn(api, "getRaceState")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue({
      session_time_ms: 500,
      drivers: [],
    } as unknown as RaceState);
  const hook = renderHook(
    ({ time, playing }) => useRaceSnapshot("race", time, playing, 0),
    { initialProps: { time: 5000, playing: true } },
  );
  hook.rerender({ time: 500, playing: false });
  await act(async () => {});
  await act(async () =>
    finish({ session_time_ms: 5000, drivers: [] } as unknown as RaceState),
  );
  expect(hook.result.current.data?.session_time_ms).toBe(500);
});
