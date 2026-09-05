"use client";

import { useEffect, useRef, useState } from "react";

import {
  ApiClientError,
  compareAnalytics,
  getAnalyticsEntities,
} from "@/lib/api/client";
import type {
  AnalyticsListItem,
  ComparisonAnalyticsResponse,
} from "@/lib/api/types";

import {
  AnalyticsPage,
  CoverageNote,
  EmptyState,
  MetricStrip,
  PageIntro,
  formatMetric,
} from "./AnalyticsPrimitives";

type EntityKind = "driver" | "constructor";
type ComparisonFilters = {
  kind: EntityKind;
  a: string;
  b: string;
  start: number;
  end: number;
};

const defaultFilters: ComparisonFilters = {
  kind: "driver",
  a: "",
  b: "",
  start: 2000,
  end: 2026,
};

function urlSeason(params: URLSearchParams, name: string, fallback: number) {
  const value = Number(params.get(name));
  return Number.isInteger(value) && value >= 2000 && value <= 2026
    ? value
    : fallback;
}

function filtersFromLocation(): ComparisonFilters {
  const params = new URLSearchParams(window.location.search);
  return {
    kind: params.get("entity") === "constructor" ? "constructor" : "driver",
    a: params.get("a") ?? "",
    b: params.get("b") ?? "",
    start: urlSeason(params, "start_season", defaultFilters.start),
    end: urlSeason(params, "end_season", defaultFilters.end),
  };
}

function filtersKey(filters: ComparisonFilters) {
  return [filters.kind, filters.a, filters.b, filters.start, filters.end].join(
    ":",
  );
}

function writeFilters(filters: ComparisonFilters, mode: "push" | "replace") {
  const url = new URL(window.location.href);
  url.searchParams.set("entity", filters.kind);
  url.searchParams.set("start_season", String(filters.start));
  url.searchParams.set("end_season", String(filters.end));
  if (filters.a) url.searchParams.set("a", filters.a);
  else url.searchParams.delete("a");
  if (filters.b) url.searchParams.set("b", filters.b);
  else url.searchParams.delete("b");
  window.history[mode === "push" ? "pushState" : "replaceState"](null, "", url);
}

function resolveSelections(
  filters: ComparisonFilters,
  entities: AnalyticsListItem[],
) {
  const contains = (id: string) =>
    entities.some((entity) => entity.entity_id === id);
  const a = contains(filters.a) ? filters.a : (entities[0]?.entity_id ?? "");
  let b = contains(filters.b) ? filters.b : "";
  if (!b)
    b = entities.find((entity) => entity.entity_id !== a)?.entity_id ?? "";
  return { ...filters, a, b };
}

export function CompareAnalytics() {
  const [filters, setFilters] = useState<ComparisonFilters>(defaultFilters);
  const filtersRef = useRef(filters);
  const directoryRef = useRef<{
    kind: EntityKind;
    items: AnalyticsListItem[];
  } | null>(null);
  const { kind, a, b, start, end } = filters;
  const requestKey = filtersKey(filters);
  const [directory, setDirectory] = useState<{
    kind: EntityKind;
    items: AnalyticsListItem[];
  } | null>(null);
  const [result, setResult] = useState<{
    key: string;
    response: ComparisonAnalyticsResponse;
  } | null>(null);
  const [requestError, setRequestError] = useState<{
    key: string;
    message: string;
  } | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const updateFromUrl = () => {
      let next = filtersFromLocation();
      const currentDirectory = directoryRef.current;
      if (currentDirectory?.kind === next.kind)
        next = resolveSelections(next, currentDirectory.items);
      filtersRef.current = next;
      setFilters(next);
    };
    updateFromUrl();
    window.addEventListener("popstate", updateFromUrl);
    return () => window.removeEventListener("popstate", updateFromUrl);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const directoryKey = `directory:${kind}`;
    void getAnalyticsEntities(
      kind === "driver" ? "drivers" : "constructors",
      { limit: 100 },
      { signal: controller.signal },
    )
      .then(async (response) => {
        const items = [...response.items];
        while (items.length < response.total) {
          const next = await getAnalyticsEntities(
            kind === "driver" ? "drivers" : "constructors",
            { offset: items.length, limit: 100 },
            { signal: controller.signal },
          );
          if (!next.items.length) break;
          items.push(...next.items);
        }
        if (controller.signal.aborted) return;
        const nextDirectory = { kind, items };
        directoryRef.current = nextDirectory;
        setDirectory(nextDirectory);
        setRequestError((current) =>
          current?.key === directoryKey ? null : current,
        );

        const current = filtersRef.current;
        if (current.kind !== kind) return;
        const resolved = resolveSelections(current, items);
        if (filtersKey(resolved) !== filtersKey(current)) {
          filtersRef.current = resolved;
          writeFilters(resolved, "replace");
          setFilters(resolved);
        }
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setRequestError({
            key: directoryKey,
            message:
              reason instanceof ApiClientError
                ? reason.message
                : "The entity register could not be loaded.",
          });
      });
    return () => controller.abort();
  }, [kind]);

  const entities = directory?.kind === kind ? directory.items : [];
  const directoryReady = directory?.kind === kind;
  const validSelection = Boolean(a && b && a !== b);

  useEffect(() => {
    if (!directoryReady || !validSelection) return;
    const controller = new AbortController();
    void compareAnalytics(
      {
        entity_type: kind,
        entity_a: a,
        entity_b: b,
        mode: "common_races",
        filters: { start_season: start, end_season: end },
      },
      { signal: controller.signal },
    )
      .then((response) => {
        if (controller.signal.aborted) return;
        setResult({ key: requestKey, response });
        setRequestError((current) =>
          current?.key === requestKey ? null : current,
        );
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted)
          setRequestError({
            key: requestKey,
            message:
              reason instanceof ApiClientError
                ? reason.message
                : "Comparison failed.",
          });
      });
    return () => controller.abort();
  }, [
    a,
    b,
    directoryReady,
    end,
    kind,
    refresh,
    requestKey,
    start,
    validSelection,
  ]);

  function updateFilters(patch: Partial<ComparisonFilters>) {
    let next = { ...filters, ...patch };
    if (patch.kind && patch.kind !== kind) next = { ...next, a: "", b: "" };
    if (filtersKey(next) === requestKey) return;
    filtersRef.current = next;
    writeFilters(next, "push");
    setFilters(next);
  }

  const data = result?.key === requestKey ? result.response : null;
  const directoryErrorKey = `directory:${kind}`;
  const error =
    !validSelection && directoryReady
      ? "Choose two different archive entities."
      : requestError?.key === requestKey ||
          requestError?.key === directoryErrorKey
        ? requestError.message
        : null;
  const summaryA =
    data?.entity_a.summary &&
    typeof data.entity_a.summary === "object" &&
    !Array.isArray(data.entity_a.summary)
      ? data.entity_a.summary
      : null;
  const summaryB =
    data?.entity_b.summary &&
    typeof data.entity_b.summary === "object" &&
    !Array.isArray(data.entity_b.summary)
      ? data.entity_b.summary
      : null;
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow="Common-race comparison / Explicit denominator"
        title="Head to head"
      >
        <p>
          Compare only races both entities contested, with non-comparable driver
          finishes excluded.
        </p>
      </PageIntro>
      <section className="analytics-filterbar analytics-filterbar--compare">
        <label>
          <span>Entity</span>
          <select
            value={kind}
            onChange={(event) =>
              updateFilters({ kind: event.target.value as EntityKind })
            }
          >
            <option value="driver">Drivers</option>
            <option value="constructor">Constructors</option>
          </select>
        </label>
        <label>
          <span>A</span>
          <select
            value={a}
            onChange={(event) => updateFilters({ a: event.target.value })}
          >
            {entities.map((entity) => (
              <option key={entity.entity_id} value={entity.entity_id}>
                {entity.entity_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>B</span>
          <select
            value={b}
            onChange={(event) => updateFilters({ b: event.target.value })}
          >
            {entities.map((entity) => (
              <option key={entity.entity_id} value={entity.entity_id}>
                {entity.entity_name}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>From</span>
          <input
            type="number"
            min="2000"
            max="2026"
            value={start}
            onChange={(event) =>
              updateFilters({ start: Number(event.target.value) })
            }
          />
        </label>
        <label>
          <span>To</span>
          <input
            type="number"
            min="2000"
            max="2026"
            value={end}
            onChange={(event) =>
              updateFilters({ end: Number(event.target.value) })
            }
          />
        </label>
        <button
          type="button"
          disabled={!validSelection}
          onClick={() => setRefresh((current) => current + 1)}
        >
          Run comparison
        </button>
      </section>
      {error ? <EmptyState>{error}</EmptyState> : null}
      {data && summaryA && summaryB ? (
        <>
          <section className="versus-header">
            <div>
              <span>A</span>
              <h2>{String(data.entity_a.entity_name)}</h2>
            </div>
            <strong>VS</strong>
            <div>
              <span>B</span>
              <h2>{String(data.entity_b.entity_name)}</h2>
            </div>
          </section>
          <MetricStrip
            metrics={[
              {
                label: `${String(data.entity_a.entity_name)} wins`,
                value: summaryA.wins,
              },
              {
                label: `${String(data.entity_b.entity_name)} wins`,
                value: summaryB.wins,
              },
              { label: "Common races", value: data.common_race_count },
              {
                label: "Comparable finishes",
                value: data.head_to_head.denominator,
              },
              {
                label: `${String(data.entity_a.entity_name)} ${kind === "driver" ? "ahead" : "race points lead"}`,
                value: data.head_to_head.a_finished_ahead,
              },
              {
                label: `${String(data.entity_b.entity_name)} ${kind === "driver" ? "ahead" : "race points lead"}`,
                value: data.head_to_head.b_finished_ahead,
              },
              ...(kind === "constructor"
                ? [
                    {
                      label: "Equal race points",
                      value: data.head_to_head.tied,
                    },
                  ]
                : []),
            ]}
          />
          <p className="deterministic-summary">
            {String(data.entity_a.entity_name)}{" "}
            {kind === "driver" ? "finished ahead" : "scored more points"} in{" "}
            {formatMetric(data.head_to_head.a_finished_ahead)} of{" "}
            {formatMetric(data.head_to_head.denominator)} comparable common
            races; {formatMetric(data.head_to_head.excluded_non_comparable)}{" "}
            were excluded.
          </p>
          <CoverageNote coverage={data.coverage} />
          {kind === "constructor" ? (
            <p className="era-warning">
              Constructor comparisons use recorded Grand Prix race-session
              points. Sprint points are outside this archive and equal-points
              races remain in the denominator.
            </p>
          ) : null}
        </>
      ) : (
        <EmptyState>
          Select two entities and run a deterministic comparison.
        </EmptyState>
      )}
    </AnalyticsPage>
  );
}
