import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { StrictMode, useEffect, useState, type ReactNode } from "react";
import { useTelemetryData } from "./useTelemetryData";
import * as client from "./client";
import type {
  Comparison,
  Trace,
  TelemetrySession,
  TelemetryLap,
} from "./types";
const revision = "a".repeat(64);
const session = {
  session_id: "test_race",
  revision,
  season: 2024,
  event: "Test-only fixture",
  circuit: null,
  session_type: "Race",
  drivers: ["1", "44"],
  available_tables: [],
  channel_availability: {},
};
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});
function install() {
  vi.spyOn(client, "getTelemetrySessions").mockResolvedValue({
    items: [session],
    total: 1,
    offset: 0,
    limit: 100,
    source: "production-canonical",
    root_configured: true,
  });
  vi.spyOn(client, "getTelemetryLaps").mockImplementation(
    async (_session, driver) => ({
      session_id: "test_race",
      revision,
      driver,
      items: [],
      total: 0,
      offset: 0,
      limit: 100,
    }),
  );
  vi.spyOn(client, "getTelemetryTrace").mockImplementation(
    async (_selection, side) =>
      ({
        session_id: "test_race",
        revision,
        driver: side === "A" ? "1" : "44",
        lap_number: 1,
      }) as Trace,
  );
  vi.spyOn(client, "getTelemetryCorners").mockRejectedValue(
    new Error("No windows in this test fixture"),
  );
  vi.spyOn(client, "getTelemetryMicrosectors").mockRejectedValue(
    new Error("No microsectors in this test fixture"),
  );
}
const contextSession: TelemetrySession = {
  ...session,
  session_id: "2024-08-race",
  session_type: "race",
  replay_session_id: "session-2024-round-08-type-race",
  race_analytics_session_id: "archive-2024-round-08-race",
};
function installContext(items = [contextSession]) {
  install();
  vi.mocked(client.getTelemetrySessions).mockResolvedValue({
    items,
    total: items.length,
    offset: 0,
    limit: 100,
    source: "production-canonical",
    root_configured: true,
  });
  vi.mocked(client.getTelemetryLaps).mockImplementation(
    async (identity, driver, pinned) => ({
      session_id: identity,
      driver,
      revision: pinned!,
      offset: 0,
      limit: 100,
      total: 1,
      items: [
        {
          driver,
          lap_number: 58,
          lap_time_seconds: 80,
          reliable: true,
          car_available: true,
          timing_available: true,
          deleted: false,
          provider_generated: false,
          pit_in: false,
          pit_out: false,
        } as TelemetryLap,
      ],
    }),
  );
  vi.mocked(client.getTelemetryTrace).mockImplementation(
    async (selection) =>
      ({
        session_id: selection.session,
        revision: selection.revision,
      }) as Trace,
  );
  vi.spyOn(client, "getTelemetryComparison").mockImplementation(
    async (selection) =>
      ({
        session_id: selection.session,
        revision: selection.revision,
      }) as Comparison,
  );
}
it("initializes from authoritative router query before the browser history commit", async () => {
  window.history.replaceState(
    null,
    "",
    "/app/analytics/races/archive-2024-round-08-race",
  );
  installContext([
    {
      ...session,
      session_id: "2018-01-practice-1",
      season: 2018,
      session_type: "practice-1",
    },
    contextSession,
  ]);
  const { result } = renderHook(() =>
    useTelemetryData(
      "race_session_id=archive-2024-round-08-race&view=performance",
    ),
  );
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  expect(result.current.selection).toMatchObject({
    session: "2024-08-race",
    revision,
    view: "performance",
  });
  expect(
    vi
      .mocked(client.getTelemetryLaps)
      .mock.calls.every(([identity]) => identity === "2024-08-race"),
  ).toBe(true);
});
it("preserves unresolved authoritative router context before browser history commit", async () => {
  window.history.replaceState(
    null,
    "",
    "/app/analytics/races/archive-2024-round-99-race",
  );
  installContext();
  const { result } = renderHook(() =>
    useTelemetryData("race_session_id=archive-2024-round-99-race"),
  );
  await waitFor(() => expect(result.current.sessions.kind).toBe("ready"));
  expect(result.current.laps.kind).toBe("error");
  expect(result.current.selection.session).toBeUndefined();
  expect(client.getTelemetryLaps).not.toHaveBeenCalled();
  expect(window.location.search).toContain(
    "race_session_id=archive-2024-round-99-race",
  );
});
it.each([
  ["race_session_id", "archive-2024-round-08-race"],
  ["replay_session_id", "session-2024-round-08-type-race"],
])(
  "resolves distinct %s into native identity and verifies incoming lap",
  async (key, id) => {
    window.history.replaceState(
      null,
      "",
      `/?${key}=${id}&driver=44&lap=58&revision=${"f".repeat(64)}&zoom=10,20`,
    );
    installContext([
      contextSession,
      {
        ...contextSession,
        session_id: "2024-08-qualifying",
        session_type: "qualifying",
        replay_session_id: null,
      },
    ]);
    const { result } = renderHook(() => useTelemetryData());
    await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
    expect(result.current.selection).toMatchObject({
      session: "2024-08-race",
      revision,
      driverA: "44",
      lapA: 58,
    });
    expect(window.location.search).toContain("session=2024-08-race");
    expect(window.location.search).not.toContain(`${key}=`);
    expect(window.location.search).not.toContain("zoom=");
    expect(client.getTelemetryComparison).toHaveBeenCalledTimes(1);
    expect(
      vi.mocked(client.getTelemetryComparison).mock.calls[0][0],
    ).toMatchObject({
      session: "2024-08-race",
      revision,
      driverA: "44",
      lapA: 58,
    });
  },
);
it.each(["missing", "ambiguous"])(
  "keeps %s incoming context honestly unavailable",
  async (kind) => {
    window.history.replaceState(
      null,
      "",
      "/?race_session_id=archive-2024-round-08-race&driver=44&lap=58",
    );
    installContext(
      kind === "missing"
        ? [session]
        : [contextSession, { ...contextSession, session_id: "duplicate-race" }],
    );
    const { result } = renderHook(() => useTelemetryData());
    await waitFor(() => expect(result.current.sessions.kind).toBe("ready"));
    expect(result.current.laps.kind).toBe("error");
    expect(result.current.selection.session).toBeUndefined();
    expect(window.location.search).toContain(
      "race_session_id=archive-2024-round-08-race",
    );
    expect(client.getTelemetryLaps).not.toHaveBeenCalled();
    expect(client.getTelemetryComparison).not.toHaveBeenCalled();
  },
);
it.each([
  ["driver=99&lap=58", "1", 58],
  ["driver=44&lap=999", "44", 58],
])(
  "only preserves verified native context for %s",
  async (query, driver, lap) => {
    window.history.replaceState(
      null,
      "",
      `/?replay_session_id=session-2024-round-08-type-race&${query}`,
    );
    installContext();
    const { result } = renderHook(() => useTelemetryData());
    await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
    expect(result.current.selection).toMatchObject({
      session: "2024-08-race",
      driverA: driver,
      lapA: lap,
    });
    expect(
      vi.mocked(client.getTelemetryComparison).mock.calls[0][0],
    ).toMatchObject({ driverA: driver, lapA: lap });
  },
);
it("resolves history context without stale native revision or mixed identity", async () => {
  window.history.replaceState(
    null,
    "",
    `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=58&lap_b=58`,
  );
  installContext([session, { ...contextSession, revision: "b".repeat(64) }]);
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  act(() => {
    window.history.replaceState(
      null,
      "",
      `/?race_session_id=archive-2024-round-08-race&driver=44&lap=58&revision=${revision}`,
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await waitFor(() =>
    expect(result.current.selection.session).toBe("2024-08-race"),
  );
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  expect(result.current.selection.revision).toBe("b".repeat(64));
  expect(
    result.current.bundle.kind === "ready" &&
      result.current.bundle.data.comparison.session_id,
  ).toBe("2024-08-race");
});
it("revalidates a history context even when it resolves to the current native identity", async () => {
  window.history.replaceState(
    null,
    "",
    `/?session=2024-08-race&revision=${revision}&driver_a=44&driver_b=44&lap_a=58&lap_b=58`,
  );
  installContext();
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  act(() => {
    window.history.replaceState(
      null,
      "",
      "/?race_session_id=archive-2024-round-08-race&driver_a=44&driver_b=44&lap_a=58&lap_b=58",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await waitFor(() =>
    expect(result.current.selection.validateContextLaps).toBeUndefined(),
  );
  expect(result.current.bundle.kind).toBe("ready");
});
it.each([
  "race_session_id=archive-2024-round-08-race&race_session_id=archive-2024-round-08-race",
  "race_session_id=archive-2024-round-08-race&replay_session_id=session-2024-round-08-type-race",
  "session=missing_race&race_session_id=archive-2024-round-08-race",
])(
  "never defaults conflicted or duplicated incoming context %s",
  async (query) => {
    window.history.replaceState(null, "", `/?${query}`);
    installContext();
    const { result } = renderHook(() => useTelemetryData());
    await waitFor(() => expect(result.current.sessions.kind).toBe("ready"));
    expect(result.current.laps.kind).toBe("error");
    expect(client.getTelemetryLaps).not.toHaveBeenCalled();
    expect(window.location.search).toContain("context_reference_invalid=1");
  },
);
it("reports an unavailable URL session without substituting the first catalog entry and retries discovery", async () => {
  window.history.replaceState(null, "", "/?session=missing_race");
  install();
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.sessions.kind).toBe("ready"));
  expect(result.current.laps).toEqual({
    kind: "error",
    message:
      'Requested telemetry session "missing_race" is unavailable in the canonical catalog. Select an available session or retry discovery.',
  });
  expect(result.current.selection.session).toBe("missing_race");
  expect(window.location.search).toContain("session=missing_race");
  expect(client.getTelemetryLaps).not.toHaveBeenCalled();
  vi.mocked(client.getTelemetrySessions).mockResolvedValue({
    items: [{ ...session, session_id: "missing_race" }],
    total: 1,
    offset: 0,
    limit: 100,
    source: "production-canonical",
    root_configured: true,
  });
  act(() => result.current.retry());
  await waitFor(() => expect(result.current.laps.kind).toBe("ready"));
  expect(client.getTelemetrySessions).toHaveBeenCalledTimes(2);
  expect(result.current.selection.session).toBe("missing_race");
});
it("automatic selection never updates URL routing during a render updater", async () => {
  install();
  const errors = vi.spyOn(console, "error").mockImplementation(() => {});
  function RouterWrapper({ children }: { children: ReactNode }) {
    const [route, setRoute] = useState("/");
    useEffect(() => {
      const original = window.history.replaceState.bind(window.history);
      const replacement = vi
        .spyOn(window.history, "replaceState")
        .mockImplementation((data, title, url) => {
          original(data, title, url);
          setRoute(String(url));
        });
      return () => replacement.mockRestore();
    }, []);
    return (
      <StrictMode>
        <div data-route={route}>{children}</div>
      </StrictMode>
    );
  }
  const { result } = renderHook(() => useTelemetryData(), {
    wrapper: RouterWrapper,
  });
  await waitFor(() => expect(result.current.laps.kind).toBe("ready"));
  expect(window.location.search).toContain("session=test_race");
  expect(
    errors.mock.calls.some((args) =>
      String(args[0]).includes("Cannot update a component"),
    ),
  ).toBe(false);
});
it("browser back changes views without refetching or getting stuck loading", async () => {
  window.history.replaceState(
    null,
    "",
    `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2&view=performance`,
  );
  install();
  const compare = vi
    .spyOn(client, "getTelemetryComparison")
    .mockResolvedValue({ session_id: "test_race", revision } as Comparison);
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  act(() => {
    window.history.replaceState(
      null,
      "",
      `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2&view=inputs`,
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(result.current.selection.view).toBe("inputs");
  expect(result.current.bundle.kind).toBe("ready");
  expect(compare).toHaveBeenCalledTimes(1);
});
it("ignores a stale completed pair after a newer selection, even if fetch ignores abort", async () => {
  window.history.replaceState(
    null,
    "",
    `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2`,
  );
  install();
  let resolveOld: (value: Comparison) => void = () => {};
  const old = new Promise<Comparison>((resolve) => {
    resolveOld = resolve;
  });
  vi.spyOn(client, "getTelemetryComparison").mockImplementation((s) =>
    s.lapB === 2
      ? old
      : Promise.resolve({
          session_id: "test_race",
          revision,
          algorithm_version: "new-pair",
        } as Comparison),
  );
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.sessions.kind).toBe("ready"));
  act(() => result.current.update({ ...result.current.selection, lapB: 3 }));
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  await act(async () => {
    resolveOld({
      session_id: "test_race",
      revision,
      algorithm_version: "stale-pair",
    } as Comparison);
    await old;
  });
  expect(
    result.current.bundle.kind === "ready" &&
      result.current.bundle.data.comparison.algorithm_version,
  ).toBe("new-pair");
});
it("restores quality opt-ins from reload and history and refetches exact settings", async () => {
  const original = `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2&allow_partial=true&allow_deleted=true`;
  window.history.replaceState(null, "", original);
  install();
  const compare = vi
    .spyOn(client, "getTelemetryComparison")
    .mockResolvedValue({ session_id: "test_race", revision } as Comparison);
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  expect(result.current.settings.allow_partial).toBe(true);
  expect(result.current.settings.allow_deleted).toBe(true);
  expect(compare.mock.calls[0][1]).toMatchObject({
    allow_partial: true,
    allow_deleted: true,
    allow_pit_laps: false,
    allow_provider_generated: false,
    allow_unreliable: false,
  });
  act(() =>
    result.current.setSettings({
      ...result.current.settings,
      allow_partial: false,
    }),
  );
  expect(window.location.search).toContain("allow_partial=false");
  await waitFor(() => expect(compare).toHaveBeenCalledTimes(2));
  act(() => {
    window.history.replaceState(null, "", original);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await waitFor(() => expect(compare).toHaveBeenCalledTimes(3));
  expect(result.current.settings.allow_partial).toBe(true);
  expect(compare.mock.calls[2][1]).toMatchObject({
    allow_partial: true,
    allow_deleted: true,
  });
});
it("invalidates stale quality requests when an opt-in changes", async () => {
  window.history.replaceState(
    null,
    "",
    `/?session=test_race&revision=${revision}&driver_a=1&driver_b=44&lap_a=1&lap_b=2`,
  );
  install();
  let resolveOld: (value: Comparison) => void = () => {};
  const old = new Promise<Comparison>((resolve) => {
    resolveOld = resolve;
  });
  const compare = vi
    .spyOn(client, "getTelemetryComparison")
    .mockImplementation((_s, settings) =>
      settings?.allow_partial
        ? Promise.resolve({
            session_id: "test_race",
            revision,
            algorithm_version: "opt-in",
          } as Comparison)
        : old,
    );
  const { result } = renderHook(() => useTelemetryData());
  await waitFor(() => expect(compare).toHaveBeenCalledTimes(1));
  act(() =>
    result.current.setSettings({
      ...result.current.settings,
      allow_partial: true,
    }),
  );
  await waitFor(() => expect(result.current.bundle.kind).toBe("ready"));
  await act(async () => {
    resolveOld({
      session_id: "test_race",
      revision,
      algorithm_version: "stale-strict",
    } as Comparison);
    await old;
  });
  expect(
    result.current.bundle.kind === "ready" &&
      result.current.bundle.data.comparison.algorithm_version,
  ).toBe("opt-in");
  expect(compare.mock.calls[0][2]?.signal?.aborted).toBe(true);
});
