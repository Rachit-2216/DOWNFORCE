import type {
  ArchiveLap,
  ArchivePage,
  ArchivePitStop,
  ArchiveResult,
  AnalyticsPageResponse,
  CatalogEvent,
  CatalogEventListResponse,
  CatalogSeasonListResponse,
  ComparisonAnalyticsResponse,
  DriverListResponse,
  IntelligenceResponse,
  EntityAnalyticsResponse,
  LapListResponse,
  RaceEventType,
  RaceState,
  RaceAnalyticsResponse,
  RankingAnalyticsResponse,
  SessionListResponse,
  SessionResponse,
  StrategyCandidate,
  StrategyComparisonResponse,
  SeasonAnalyticsResponse,
  TimelineResponse,
  TrackPositionListResponse,
} from "./types";

export type HealthResponse = {
  status: "ok";
  service: string;
  version: string;
};

export type ApiRequestOptions = {
  baseUrl?: string;
  signal?: AbortSignal;
  fetcher?: typeof fetch;
};

export class ApiClientError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    options?: ErrorOptions,
  ) {
    super(message, options);
    this.name = "ApiClientError";
  }
}

export function resolveApiBaseUrl(
  configuredValue = process.env.NEXT_PUBLIC_API_URL,
  environment = process.env.NODE_ENV,
): string {
  const configuredUrl = configuredValue?.trim();
  if (configuredUrl) {
    return configuredUrl.replace(/\/$/, "");
  }

  if (environment === "development" || environment === "test") {
    return "http://127.0.0.1:8000";
  }

  throw new ApiClientError(
    "NEXT_PUBLIC_API_URL is not configured. Set it to the DOWNFORCE API origin.",
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (!isRecord(value)) {
    return false;
  }

  const candidate = value;
  return (
    candidate.status === "ok" &&
    typeof candidate.service === "string" &&
    typeof candidate.version === "string"
  );
}

export async function getHealth(
  options: ApiRequestOptions = {},
): Promise<HealthResponse> {
  const fetcher = options.fetcher ?? fetch;
  const baseUrl = options.baseUrl ?? resolveApiBaseUrl();
  let response: Response;

  try {
    response = await fetcher(`${baseUrl}/health`, {
      headers: { Accept: "application/json" },
      signal: options.signal,
    });
  } catch (error: unknown) {
    throw new ApiClientError(
      `Unable to reach the DOWNFORCE API at ${baseUrl}. Start the backend and verify NEXT_PUBLIC_API_URL.`,
      undefined,
      { cause: error },
    );
  }

  if (!response.ok) {
    throw new ApiClientError(
      `DOWNFORCE API health check returned HTTP ${response.status}.`,
      response.status,
    );
  }

  const payload: unknown = await response.json();
  if (!isHealthResponse(payload)) {
    throw new ApiClientError(
      "DOWNFORCE API returned an invalid health response.",
      response.status,
    );
  }

  return payload;
}

async function requestJson<T>(
  path: string,
  options: ApiRequestOptions & { method?: "GET" | "POST"; body?: unknown } = {},
): Promise<T> {
  const fetcher = options.fetcher ?? fetch;
  const baseUrl = options.baseUrl ?? resolveApiBaseUrl();
  let response: Response;

  try {
    response = await fetcher(`${baseUrl}${path}`, {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        ...(options.body === undefined
          ? {}
          : { "Content-Type": "application/json" }),
      },
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (error: unknown) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiClientError(
      `Unable to reach the DOWNFORCE API at ${baseUrl}.`,
      undefined,
      { cause: error },
    );
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload: unknown = await response.json();
      if (isRecord(payload)) {
        const error = payload.error;
        const nestedMessage = isRecord(error) ? error.message : undefined;
        const directDetail = payload.detail;
        if (typeof nestedMessage === "string") detail = nestedMessage;
        else if (typeof directDetail === "string") detail = directDetail;
      }
    } catch {
      // The status code remains the truthful fallback for non-JSON failures.
    }
    throw new ApiClientError(
      `DOWNFORCE API request failed: ${detail}.`,
      response.status,
    );
  }

  return (await response.json()) as T;
}

function queryString(
  values: Record<string, string | number | undefined>,
): string {
  const query = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined) query.set(key, String(value));
  });
  const serialized = query.toString();
  return serialized ? `?${serialized}` : "";
}

function sessionPath(sessionId: string): string {
  return `/api/v1/sessions/${encodeURIComponent(sessionId)}`;
}

export function getSessions(
  options: ApiRequestOptions = {},
): Promise<SessionListResponse> {
  return requestJson("/api/v1/sessions?limit=100", options);
}

export function getCatalogSeasons(
  options: ApiRequestOptions = {},
): Promise<CatalogSeasonListResponse> {
  return requestJson("/api/v1/catalog/seasons", options);
}

export function getCatalogEvents(
  params: {
    season?: number;
    query?: string;
    circuit?: string;
    driver?: string;
    team?: string;
    capability?: string;
    status?: string;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
): Promise<CatalogEventListResponse> {
  const query = queryString({
    season: params.season,
    query: params.query,
    circuit: params.circuit,
    driver: params.driver,
    team: params.team,
    capability: params.capability,
    status: params.status,
    offset: params.offset,
    limit: params.limit ?? 100,
  });
  return requestJson(`/api/v1/catalog/events${query}`, options);
}

export function getCatalogEvent(
  eventId: string,
  options: ApiRequestOptions = {},
): Promise<CatalogEvent> {
  return requestJson(
    `/api/v1/catalog/events/${encodeURIComponent(eventId)}`,
    options,
  );
}

function archiveSessionPath(sessionId: string): string {
  return `/api/v1/catalog/sessions/${encodeURIComponent(sessionId)}`;
}

export function getArchiveResults(
  sessionId: string,
  params: { driverId?: string; offset?: number; limit?: number } = {},
  options: ApiRequestOptions = {},
): Promise<ArchivePage<ArchiveResult>> {
  const query = queryString({
    driver_id: params.driverId,
    offset: params.offset,
    limit: params.limit ?? 100,
  });
  return requestJson(
    `${archiveSessionPath(sessionId)}/results${query}`,
    options,
  );
}

export function getArchiveLaps(
  sessionId: string,
  params: {
    driverId?: string;
    fromLap?: number;
    toLap?: number;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
): Promise<ArchivePage<ArchiveLap>> {
  const query = queryString({
    driver_id: params.driverId,
    from_lap: params.fromLap,
    to_lap: params.toLap,
    offset: params.offset,
    limit: params.limit ?? 1_000,
  });
  return requestJson(`${archiveSessionPath(sessionId)}/laps${query}`, options);
}

export function getArchivePitStops(
  sessionId: string,
  params: { driverId?: string; offset?: number; limit?: number } = {},
  options: ApiRequestOptions = {},
): Promise<ArchivePage<ArchivePitStop>> {
  const query = queryString({
    driver_id: params.driverId,
    offset: params.offset,
    limit: params.limit ?? 500,
  });
  return requestJson(
    `${archiveSessionPath(sessionId)}/pit-stops${query}`,
    options,
  );
}

export function getSession(
  sessionId: string,
  options: ApiRequestOptions = {},
): Promise<SessionResponse> {
  return requestJson(sessionPath(sessionId), options);
}

export function getDrivers(
  sessionId: string,
  options: ApiRequestOptions = {},
): Promise<DriverListResponse> {
  return requestJson(`${sessionPath(sessionId)}/drivers`, options);
}

export function getLaps(
  sessionId: string,
  params: {
    driverId?: string;
    fromLap?: number;
    toLap?: number;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
): Promise<LapListResponse> {
  const query = queryString({
    driver_id: params.driverId,
    from_lap: params.fromLap,
    to_lap: params.toLap,
    offset: params.offset,
    limit: params.limit ?? 1_000,
  });
  return requestJson(`${sessionPath(sessionId)}/laps${query}`, options);
}

export function getTimeline(
  sessionId: string,
  params: {
    fromMs?: number;
    toMs?: number;
    types?: RaceEventType[];
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
): Promise<TimelineResponse> {
  const query = new URLSearchParams();
  if (params.fromMs !== undefined) query.set("from_ms", String(params.fromMs));
  if (params.toMs !== undefined) query.set("to_ms", String(params.toMs));
  params.types?.forEach((type) => query.append("types", type));
  query.set("offset", String(params.offset ?? 0));
  query.set("limit", String(params.limit ?? 1_000));
  return requestJson(`${sessionPath(sessionId)}/timeline?${query}`, options);
}

export function getRaceState(
  sessionId: string,
  cursor: { timeMs: number } | { lap: number; phase?: "start" | "end" },
  options: ApiRequestOptions = {},
): Promise<RaceState> {
  const query =
    "timeMs" in cursor
      ? queryString({ time_ms: Math.max(0, Math.round(cursor.timeMs)) })
      : queryString({ lap: cursor.lap, phase: cursor.phase ?? "end" });
  return requestJson(`${sessionPath(sessionId)}/state${query}`, options);
}

export function getTrackPositions(
  sessionId: string,
  params: {
    driverId?: string;
    fromMs?: number;
    toMs?: number;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
): Promise<TrackPositionListResponse> {
  const query = queryString({
    driver_id: params.driverId,
    from_ms: params.fromMs,
    to_ms: params.toMs,
    offset: params.offset,
    limit: params.limit ?? 5_000,
  });
  return requestJson(
    `${sessionPath(sessionId)}/track-positions${query}`,
    options,
  );
}

export function getIntelligence(
  sessionId: string,
  driverId: string,
  timeMs: number,
  options: ApiRequestOptions = {},
): Promise<IntelligenceResponse> {
  const query = queryString({ time_ms: Math.max(0, Math.round(timeMs)) });
  return requestJson(
    `${sessionPath(sessionId)}/drivers/${encodeURIComponent(driverId)}/intelligence${query}`,
    options,
  );
}

export function compareStrategies(
  sessionId: string,
  request: {
    cursor_time_ms: number;
    driver_id: string;
    strategies: StrategyCandidate[];
    scenario: {
      scheduled_total_laps?: number;
      pit_loss_mode: "sampled";
      require_two_compounds: boolean;
    };
    simulation_count: number;
    seed: number;
  },
  options: ApiRequestOptions = {},
): Promise<StrategyComparisonResponse> {
  return requestJson(`${sessionPath(sessionId)}/strategy/compare`, {
    ...options,
    method: "POST",
    body: request,
  });
}

export type AnalyticsFilters = {
  startSeason?: number;
  endSeason?: number;
  circuitId?: string;
};

function analyticsQuery(
  filters: AnalyticsFilters = {},
  extras: Record<string, string | number | undefined> = {},
) {
  return queryString({
    start_season: filters.startSeason,
    end_season: filters.endSeason,
    circuit_id: filters.circuitId,
    ...extras,
  });
}

export function getSeasonAnalytics(
  year: number,
  options: ApiRequestOptions = {},
) {
  return requestJson<SeasonAnalyticsResponse>(
    `/api/v1/analytics/seasons/${year}`,
    options,
  );
}

export function getAnalyticsEntities(
  entity: "drivers" | "constructors" | "circuits",
  filters: AnalyticsFilters & {
    search?: string;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
) {
  return requestJson<AnalyticsPageResponse>(
    `/api/v1/analytics/${entity}${analyticsQuery(filters, { search: filters.search, offset: filters.offset, limit: filters.limit ?? 50 })}`,
    options,
  );
}

export function getEntityAnalytics(
  entity: "drivers" | "constructors" | "circuits",
  id: string,
  filters: AnalyticsFilters & { offset?: number; limit?: number } = {},
  options: ApiRequestOptions = {},
) {
  return requestJson<EntityAnalyticsResponse>(
    `/api/v1/analytics/${entity}/${encodeURIComponent(id)}${analyticsQuery(
      filters,
      {
        offset: filters.offset,
        limit: filters.limit,
      },
    )}`,
    options,
  );
}

export function getRaceAnalytics(
  sessionId: string,
  options: ApiRequestOptions = {},
) {
  return requestJson<RaceAnalyticsResponse>(
    `/api/v1/analytics/races/${encodeURIComponent(sessionId)}`,
    options,
  );
}

export function compareAnalytics(
  request: {
    entity_type: "driver" | "constructor";
    entity_a: string;
    entity_b: string;
    mode: "common_races" | "all_selected_races";
    filters: { start_season: number; end_season: number; circuit_id?: string };
  },
  options: ApiRequestOptions = {},
) {
  return requestJson<ComparisonAnalyticsResponse>("/api/v1/analytics/compare", {
    ...options,
    method: "POST",
    body: request,
  });
}

export function getAnalyticsRankings(
  params: AnalyticsFilters & {
    entityType?: "driver" | "constructor";
    metric?: string;
    minimumStarts?: number;
    offset?: number;
    limit?: number;
  } = {},
  options: ApiRequestOptions = {},
) {
  return requestJson<RankingAnalyticsResponse>(
    `/api/v1/analytics/rankings${analyticsQuery(params, {
      entity_type: params.entityType,
      metric: params.metric,
      minimum_starts: params.minimumStarts,
      offset: params.offset,
      limit: params.limit ?? 50,
    })}`,
    options,
  );
}
