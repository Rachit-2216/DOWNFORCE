"use client";
import { useEffect, useRef, useState } from "react";
import {
  getTelemetryComparison,
  getTelemetryCorners,
  getTelemetryLaps,
  getTelemetryMicrosectors,
  getTelemetrySessions,
  getTelemetryTrace,
} from "./client";
import { fastestValidLap, RequestEpoch } from "./telemetry-utils";
import {
  parseTelemetryUrl,
  telemetryQuery,
  resolveTelemetryContext,
} from "./url-state";
import type {
  PreparationSettings,
  TelemetryBundle,
  TelemetryLap,
  TelemetrySelection,
  TelemetrySession,
} from "./types";
import { QUALITY_FLAGS } from "./types";

export type Load<T> =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: T };
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Canonical telemetry could not be loaded.";
export function useTelemetryData(initialSearch?: string) {
  const [selection, setSelection] = useState<TelemetrySelection>(() =>
    initialSearch !== undefined
      ? parseTelemetryUrl(initialSearch)
      : typeof window === "undefined"
        ? { view: "overview" }
        : parseTelemetryUrl(window.location.search),
  );
  const [sessions, setSessions] = useState<Load<TelemetrySession[]>>({
    kind: "loading",
  });
  const [rootConfigured, setRootConfigured] = useState(true);
  const [laps, setLaps] = useState<
    Load<{ a: TelemetryLap[]; b: TelemetryLap[] }>
  >({ kind: "loading" });
  const [bundle, setBundle] = useState<Load<TelemetryBundle>>({
    kind: "loading",
  });
  const settings: PreparationSettings = {
    allow_partial: selection.allow_partial ?? false,
    allow_pit_laps: selection.allow_pit_laps ?? false,
    allow_deleted: selection.allow_deleted ?? false,
    allow_provider_generated: selection.allow_provider_generated ?? false,
    allow_unreliable: selection.allow_unreliable ?? false,
  };
  const [retry, setRetry] = useState(0);
  const [contextRequest, setContextRequest] = useState(0);
  const epoch = useRef(new RequestEpoch());
  useEffect(() => {
    const href = `${window.location.pathname}${telemetryQuery(selection)}`;
    if (`${window.location.pathname}${window.location.search}` !== href)
      window.history.replaceState(null, "", href);
  }, [selection]);
  useEffect(() => {
    const pop = () => {
      const parsed = parseTelemetryUrl(window.location.search);
      const next =
        sessions.kind === "ready"
          ? (resolveTelemetryContext(parsed, sessions.data) ?? parsed)
          : parsed;
      if (next.validateContextLaps) setContextRequest((value) => value + 1);
      if (
        ["session", "revision", "driverA", "driverB"].some(
          (k) =>
            next[k as keyof TelemetrySelection] !==
            selection[k as keyof TelemetrySelection],
        )
      )
        setLaps({ kind: "loading" });
      if (
        ["session", "revision", "driverA", "driverB", "lapA", "lapB"].some(
          (k) =>
            next[k as keyof TelemetrySelection] !==
            selection[k as keyof TelemetrySelection],
        ) ||
        QUALITY_FLAGS.some(
          (flag) => (next[flag] ?? false) !== (selection[flag] ?? false),
        )
      ) {
        epoch.current.begin();
        setBundle({ kind: "loading" });
      }
      setSelection(next);
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [selection, sessions]);
  function update(next: TelemetrySelection, replace = false) {
    if (
      ["session", "revision", "driverA", "driverB"].some(
        (k) =>
          next[k as keyof TelemetrySelection] !==
          selection[k as keyof TelemetrySelection],
      )
    )
      setLaps({ kind: "loading" });
    if (
      ["session", "revision", "driverA", "driverB", "lapA", "lapB"].some(
        (k) =>
          next[k as keyof TelemetrySelection] !==
          selection[k as keyof TelemetrySelection],
      ) ||
      QUALITY_FLAGS.some(
        (flag) => (next[flag] ?? false) !== (selection[flag] ?? false),
      )
    ) {
      epoch.current.begin();
      setBundle({ kind: "loading" });
    }
    setSelection(next);
    window.history[replace ? "replaceState" : "pushState"](
      null,
      "",
      `${window.location.pathname}${telemetryQuery(next)}`,
    );
  }
  useEffect(() => {
    const abort = new AbortController();
    void (async () => {
      const first = await getTelemetrySessions({}, { signal: abort.signal });
      const all = [...first.items];
      for (
        let offset = 100;
        offset < Math.min(first.total, 10000);
        offset += 100
      )
        all.push(
          ...(await getTelemetrySessions({ offset }, { signal: abort.signal }))
            .items,
        );
      if (abort.signal.aborted) return;
      setRootConfigured(first.root_configured);
      setSessions({ kind: "ready", data: all });
      setSelection((previous) => {
        if (previous.contextReference)
          return resolveTelemetryContext(previous, all) ?? previous;
        const current =
          all.find((s) => s.session_id === previous.session) ??
          (!previous.session ? all[0] : undefined);
        if (!current) return previous;
        const next = {
          ...previous,
          session: current.session_id,
          revision: previous.revision ?? current.revision,
          driverA: previous.driverA ?? current.drivers[0],
          driverB: previous.driverB ?? current.drivers[1] ?? current.drivers[0],
        };
        return next;
      });
    })().catch((e) => {
      if (!abort.signal.aborted)
        setSessions({ kind: "error", message: message(e) });
    });
    return () => abort.abort();
  }, [retry]);
  const {
    session,
    revision,
    driverA,
    driverB,
    lapA,
    lapB,
    validateContextLaps,
  } = selection;
  const catalogReady = sessions.kind === "ready";
  const unavailableSession =
    catalogReady &&
    session !== undefined &&
    !sessions.data.some((item) => item.session_id === session);
  const unavailableContext =
    catalogReady && selection.contextReference !== undefined;
  const unavailableContextSelection = {
    kind: "error" as const,
    message:
      "Requested telemetry context is unavailable or ambiguous in the verified canonical catalog. Select an available native session or retry discovery.",
  };
  const unavailableSelection = {
    kind: "error" as const,
    message: `Requested telemetry session "${session}" is unavailable in the canonical catalog. Select an available session or retry discovery.`,
  };
  useEffect(() => {
    if (
      !catalogReady ||
      unavailableSession ||
      unavailableContext ||
      !session ||
      !revision ||
      !driverA ||
      !driverB
    )
      return;
    const abort = new AbortController();
    async function load(driver: string) {
      const first = await getTelemetryLaps(session!, driver, revision!, 0, {
        signal: abort.signal,
      });
      const items = [...first.items];
      for (
        let offset = 100;
        offset < Math.min(first.total, 1000);
        offset += 100
      )
        items.push(
          ...(
            await getTelemetryLaps(session!, driver, revision!, offset, {
              signal: abort.signal,
            })
          ).items,
        );
      return items;
    }
    void Promise.all([
      load(driverA),
      driverA === driverB ? load(driverA) : load(driverB),
    ])
      .then(([a, b]) => {
        if (abort.signal.aborted) return;
        setLaps({ kind: "ready", data: { a, b } });
        setSelection((previous) => {
          const next = {
            ...previous,
            validateContextLaps: undefined,
            lapA: previous.validateContextLaps
              ? a.some(
                  (lap) =>
                    lap.driver === driverA && lap.lap_number === previous.lapA,
                )
                ? previous.lapA
                : fastestValidLap(a)
              : (previous.lapA ?? fastestValidLap(a)),
            lapB: previous.validateContextLaps
              ? b.some(
                  (lap) =>
                    lap.driver === driverB && lap.lap_number === previous.lapB,
                )
                ? previous.lapB
                : fastestValidLap(b)
              : (previous.lapB ?? fastestValidLap(b)),
          };
          return next;
        });
      })
      .catch((e) => {
        if (!abort.signal.aborted)
          setLaps({ kind: "error", message: message(e) });
      });
    return () => abort.abort();
  }, [
    session,
    revision,
    driverA,
    driverB,
    retry,
    catalogReady,
    unavailableSession,
    unavailableContext,
    contextRequest,
  ]);
  const settingsKey = JSON.stringify(settings);
  useEffect(() => {
    const requests = epoch.current;
    const key = requests.begin();
    const abort = new AbortController();
    if (
      !catalogReady ||
      unavailableSession ||
      unavailableContext ||
      validateContextLaps ||
      !session ||
      !revision ||
      !driverA ||
      !driverB ||
      !lapA ||
      !lapB
    )
      return () => abort.abort();
    const pair: TelemetrySelection = {
      session,
      revision,
      driverA,
      driverB,
      lapA,
      lapB,
      view: "overview",
    };
    const preparation: PreparationSettings = JSON.parse(settingsKey);
    void (async () => {
      const options = { signal: abort.signal };
      const [comparison, traceA, traceB] = await Promise.all([
        getTelemetryComparison(pair, preparation, options),
        getTelemetryTrace(pair, "A", preparation, options),
        getTelemetryTrace(pair, "B", preparation, options),
      ]);
      const extras = await Promise.allSettled([
        getTelemetryCorners(pair, preparation, options),
        getTelemetryMicrosectors(pair, preparation, options),
      ]);
      if (abort.signal.aborted || !requests.current(key)) return;
      if (
        [
          comparison,
          traceA,
          traceB,
          ...extras.filter((x) => x.status === "fulfilled").map((x) => x.value),
        ].some((x) => x.revision !== revision || x.session_id !== session)
      )
        throw new Error(
          "Telemetry revision changed while loading. Select the current canonical revision and retry.",
        );
      setBundle({
        kind: "ready",
        data: {
          comparison,
          traceA,
          traceB,
          corners: extras[0].status === "fulfilled" ? extras[0].value : null,
          microsectors:
            extras[1].status === "fulfilled" ? extras[1].value : null,
          supplementalErrors: extras
            .filter((x) => x.status === "rejected")
            .map((x) => message(x.reason)),
        },
      });
    })().catch((e) => {
      if (!abort.signal.aborted && requests.current(key))
        setBundle({ kind: "error", message: message(e) });
    });
    return () => {
      abort.abort();
      requests.begin();
    };
  }, [
    session,
    revision,
    driverA,
    driverB,
    lapA,
    lapB,
    settingsKey,
    retry,
    catalogReady,
    unavailableSession,
    unavailableContext,
    validateContextLaps,
  ]);
  return {
    selection,
    update,
    sessions,
    rootConfigured,
    laps: unavailableContext
      ? unavailableContextSelection
      : unavailableSession
        ? unavailableSelection
        : laps,
    bundle: unavailableContext
      ? unavailableContextSelection
      : unavailableSession
        ? unavailableSelection
        : bundle,
    settings,
    setSettings: (next: PreparationSettings) => {
      update({ ...selection, ...next });
    },
    retry: () => {
      setSessions({ kind: "loading" });
      setLaps({ kind: "loading" });
      setBundle({ kind: "loading" });
      setRetry((v) => v + 1);
    },
  };
}
