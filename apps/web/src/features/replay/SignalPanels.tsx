import { memo } from "react";

import type { RaceControlState, WeatherState } from "@/lib/api/types";

import { formatClock, formatMetric } from "./replay-utils";

export const WeatherPanel = memo(function WeatherPanel({
  weather,
  availability = "available",
}: {
  weather: WeatherState | null;
  availability?: string;
}) {
  const unavailable = availability !== "available";
  return (
    <section className="signal-panel" aria-labelledby="weather-title">
      <header>
        <h2 id="weather-title">Weather</h2>
        {weather && <span>@ {formatClock(weather.observed_at_ms)}</span>}
      </header>
      {weather ? (
        <div className="weather-grid">
          <div>
            <small>Track</small>
            <strong>{formatMetric(weather.track_temperature_c, "°")}</strong>
          </div>
          <div>
            <small>Air</small>
            <strong>{formatMetric(weather.air_temperature_c, "°")}</strong>
          </div>
          <div>
            <small>Wind</small>
            <strong>{formatMetric(weather.wind_speed_mps, " m/s")}</strong>
          </div>
          <div>
            <small>Rain</small>
            <strong className={weather.rainfall ? "signal-alert" : ""}>
              {weather.rainfall === null
                ? "—"
                : weather.rainfall
                  ? "Yes"
                  : "No"}
            </strong>
          </div>
        </div>
      ) : unavailable ? (
        <div className="panel-empty panel-empty--compact">
          Weather data unavailable for this session ({availability}).
        </div>
      ) : (
        <div className="panel-empty panel-empty--compact">
          No weather observation yet.
        </div>
      )}
    </section>
  );
});

export const RaceControlPanel = memo(function RaceControlPanel({
  messages,
  availability = "available",
}: {
  messages: RaceControlState[];
  availability?: string;
}) {
  const latest = messages.slice(-4).reverse();
  const unavailable = availability !== "available";
  return (
    <section
      className="signal-panel race-control-panel"
      aria-labelledby="race-control-title"
    >
      <header>
        <h2 id="race-control-title">Race control</h2>
        <span>Latest ≤ cursor</span>
      </header>
      {latest.length ? (
        <ol>
          {latest.map((message) => (
            <li key={`${message.observed_at_ms}-${message.message}`}>
              <time>{formatClock(message.observed_at_ms)}</time>
              <span>{message.message}</span>
            </li>
          ))}
        </ol>
      ) : unavailable ? (
        <div className="panel-empty panel-empty--compact">
          Race-control data unavailable for this session ({availability}).
        </div>
      ) : (
        <div className="panel-empty panel-empty--compact">
          No race-control message yet.
        </div>
      )}
    </section>
  );
});
