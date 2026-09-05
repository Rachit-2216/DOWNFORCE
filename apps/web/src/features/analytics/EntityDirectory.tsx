"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiClientError, getAnalyticsEntities } from "@/lib/api/client";
import type { AnalyticsCoverage, AnalyticsListItem } from "@/lib/api/types";

import {
  AnalyticsPage,
  CoverageNote,
  EmptyState,
  MetricStrip,
  PageIntro,
} from "./AnalyticsPrimitives";

type EntityKind = "drivers" | "constructors" | "circuits";

export function EntityDirectory({ kind }: { kind: EntityKind }) {
  const [search, setSearch] = useState("");
  const [items, setItems] = useState<AnalyticsListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [coverage, setCoverage] = useState<AnalyticsCoverage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => {
        setLoading(true);
        setError(null);
        void getAnalyticsEntities(
          kind,
          { search: search.trim() || undefined, limit: 100 },
          { signal: controller.signal },
        )
          .then((response) => {
            setItems(response.items);
            setTotal(response.total);
            setCoverage(response.coverage);
          })
          .catch((reason: unknown) => {
            if (!controller.signal.aborted)
              setError(
                reason instanceof ApiClientError
                  ? reason.message
                  : "Analytics could not be loaded.",
              );
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          });
      },
      search ? 160 : 0,
    );
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [kind, search]);

  const singular =
    kind === "drivers"
      ? "driver"
      : kind === "constructors"
        ? "constructor"
        : "circuit";
  async function loadMore() {
    if (loadingMore || items.length >= total) return;
    setLoadingMore(true);
    setError(null);
    try {
      const response = await getAnalyticsEntities(kind, {
        search: search.trim() || undefined,
        offset: items.length,
        limit: 100,
      });
      setItems((current) => [...current, ...response.items]);
      setTotal(response.total);
      setCoverage(response.coverage);
    } catch (reason: unknown) {
      setError(
        reason instanceof ApiClientError
          ? reason.message
          : `More ${kind} could not be loaded.`,
      );
    } finally {
      setLoadingMore(false);
    }
  }
  return (
    <AnalyticsPage>
      <PageIntro
        eyebrow={`Archive entity register / ${total || "…"} records`}
        title={kind}
      >
        <p>
          Search provider-stable {singular} identities, then inspect the
          complete historical record.
        </p>
      </PageIntro>
      <section className="analytics-filterbar" aria-label={`${kind} filters`}>
        <label>
          <span>Search {kind}</span>
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={`Search by ${singular} name or ID`}
          />
        </label>
      </section>
      <section className="entity-register" aria-live="polite">
        {loading ? <EmptyState>Loading {kind}…</EmptyState> : null}
        {error ? <EmptyState>{error}</EmptyState> : null}
        {!loading && !error && !items.length ? (
          <EmptyState>No matching {kind}.</EmptyState>
        ) : null}
        {items.map((item) => (
          <Link
            key={item.entity_id}
            href={`/app/analytics/${kind}/${item.entity_id}`}
          >
            <header>
              <span>
                {item.start_season}—{item.end_season}
              </span>
              <h2>{item.entity_name}</h2>
              <code>{item.entity_id}</code>
            </header>
            {kind === "circuits" ? (
              <MetricStrip
                metrics={[
                  { label: "Races", value: item.race_count },
                  { label: "Winners", value: item.different_winners },
                  { label: "Pit coverage", value: item.pit_coverage_races },
                ]}
              />
            ) : (
              <MetricStrip
                metrics={[
                  { label: "Starts", value: item.starts },
                  { label: "Wins", value: item.wins },
                  { label: "Podiums", value: item.podiums },
                  { label: "Points", value: item.points },
                ]}
              />
            )}
          </Link>
        ))}
        {total > items.length ? (
          <button
            className="analytics-load-more"
            type="button"
            disabled={loadingMore}
            onClick={() => void loadMore()}
          >
            {loadingMore
              ? `Loading more ${kind}…`
              : `Load next ${kind} · ${items.length} of ${total}`}
          </button>
        ) : null}
        <CoverageNote coverage={coverage ?? undefined} />
      </section>
    </AnalyticsPage>
  );
}
