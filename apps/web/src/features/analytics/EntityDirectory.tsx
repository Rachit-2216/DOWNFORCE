"use client";

import Link from "next/link";
import { useState } from "react";

import { getAnalyticsEntities } from "@/lib/api/client";
import { useAsyncResource } from "@/lib/use-async-resource";
import { AsyncState } from "@/components/ui/AsyncState";

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
  const { data, error, loading, retry, updateData, requestKey } =
    useAsyncResource(`directory:${kind}:${search.trim()}`, (signal) =>
      getAnalyticsEntities(
        kind,
        { search: search.trim() || undefined, limit: 100 },
        { signal },
      ),
    );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;
  const coverage = data?.coverage;
  const [loadingMoreKey, setLoadingMoreKey] = useState<
    typeof requestKey | null
  >(null);
  const [pageError, setPageError] = useState<{
    key: typeof requestKey;
    message: string;
  } | null>(null);
  const loadingMore = loadingMoreKey === requestKey;

  const singular =
    kind === "drivers"
      ? "driver"
      : kind === "constructors"
        ? "constructor"
        : "circuit";
  async function loadMore() {
    if (loadingMore || items.length >= total) return;
    setLoadingMoreKey(requestKey);
    setPageError(null);
    try {
      const response = await getAnalyticsEntities(kind, {
        search: search.trim() || undefined,
        offset: items.length,
        limit: 100,
      });
      updateData((current) => ({
        ...response,
        items: [...current.items, ...response.items],
      }));
    } catch (reason: unknown) {
      setPageError({
        key: requestKey,
        message:
          reason instanceof Error
            ? reason.message
            : `More ${kind} could not be loaded.`,
      });
    } finally {
      setLoadingMoreKey((current) => (current === requestKey ? null : current));
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
        {loading ? (
          <AsyncState kind="loading" title={`Loading ${kind}…`} />
        ) : null}
        {error ? (
          <AsyncState
            kind="error"
            title="The register could not be loaded"
            detail={error}
            onRetry={retry}
          />
        ) : null}
        {pageError?.key === requestKey ? (
          <AsyncState
            kind="error"
            title="More records could not be loaded"
            detail={pageError.message}
            onRetry={() => void loadMore()}
          />
        ) : null}
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
