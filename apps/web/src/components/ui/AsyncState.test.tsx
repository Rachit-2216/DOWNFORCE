import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AsyncState } from "./AsyncState";

afterEach(cleanup);
it("keeps a working exit available while loading", () => {
  render(
    <AsyncState
      kind="loading"
      title="Loading race timing"
      detail="The race record is being retrieved."
      href="/app/replays"
      linkLabel="Choose another race"
    />,
  );
  expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
  expect(
    screen.getByRole("link", { name: "Choose another race" }),
  ).toHaveAttribute("href", "/app/replays");
});
it("offers an operable retry without treating a failed request as missing data", () => {
  const retry = vi.fn();
  render(
    <AsyncState
      kind="error"
      title="Timing could not be loaded"
      onRetry={retry}
    />,
  );
  expect(screen.getByRole("alert")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(retry).toHaveBeenCalledOnce();
});
it("distinguishes unsupported coverage from a request error", () => {
  render(
    <AsyncState
      kind="unavailable"
      title="No weather observations in this race"
      href="/app/replays"
      linkLabel="Explore detailed races"
    />,
  );
  expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "false");
  expect(
    screen.queryByRole("button", { name: "Try again" }),
  ).not.toBeInTheDocument();
});
