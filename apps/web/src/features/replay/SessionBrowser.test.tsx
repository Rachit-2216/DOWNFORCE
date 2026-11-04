import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";
import { getSessions } from "@/lib/api/client";
import type { SessionListResponse, SessionSummary } from "@/lib/api/types";
import { SessionBrowser } from "./SessionBrowser";

vi.mock("@/lib/api/client", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getSessions: vi.fn(),
}));
const sessions: SessionSummary[] = Array.from({ length: 13 }, (_, i) => ({
  session_id: `race-${i}`,
  dataset_id: `data-${i}`,
  season: i < 6 ? 2023 : 2024,
  event_name: `Grand Prix ${i}`,
  session_type: "race",
  provider: "FastF1",
  created_at_utc: "2026-08-23",
}));
const response: SessionListResponse = {
  items: sessions,
  total: 13,
  offset: 0,
  limit: 100,
};
async function firstRaceLink() {
  return (await screen.findByText("Grand Prix 0")).closest("a");
}
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("detailed race library", () => {
  it("keeps intelligence URL intent when opening an actual session", async () => {
    window.history.replaceState(null, "", "/app/replays?mode=intelligence");
    vi.mocked(getSessions).mockResolvedValue(response);
    render(<SessionBrowser />);
    expect(await firstRaceLink()).toHaveAttribute(
      "href",
      "/app/replay/race-0?mode=intelligence",
    );
    expect(
      screen.getByRole("button", { name: "Intelligence" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
  it("falls back from invalid mode and supports mode changes and browser back", async () => {
    window.history.replaceState(null, "", "/app/replays?mode=invalid");
    vi.mocked(getSessions).mockResolvedValue(response);
    render(<SessionBrowser />);
    expect(await firstRaceLink()).toHaveAttribute(
      "href",
      "/app/replay/race-0?mode=replay",
    );
    fireEvent.click(screen.getByRole("button", { name: "Strategy" }));
    expect(window.location.search).toBe("?mode=strategy");
    expect(await firstRaceLink()).toHaveAttribute(
      "href",
      "/app/replay/race-0?mode=strategy",
    );
    act(() => {
      window.history.replaceState(null, "", "/app/replays?mode=intelligence");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(
      screen.getByRole("button", { name: "Intelligence" }),
    ).toHaveAttribute("aria-pressed", "true");
  });
  it("keeps mode through retry and gives online recovery without setup commands", async () => {
    window.history.replaceState(null, "", "/app/replays?mode=strategy");
    vi.mocked(getSessions)
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(response);
    render(<SessionBrowser />);
    const retry = await screen.findByRole("button", { name: "Retry races" });
    expect(screen.queryByText(/pnpm|ingest|setup/i)).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Explore archive" }),
    ).toHaveAttribute("href", "/app");
    fireEvent.click(retry);
    expect(await firstRaceLink()).toHaveAttribute(
      "href",
      "/app/replay/race-0?mode=strategy",
    );
  });
  it("filters the real library without changing its published count or losing races", async () => {
    vi.mocked(getSessions).mockResolvedValue(response);
    render(<SessionBrowser />);
    await firstRaceLink();
    expect(screen.getByRole("status")).toHaveTextContent(
      "13 of 13 published races",
    );
    fireEvent.change(screen.getByLabelText("Season"), {
      target: { value: "2023" },
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "6 of 13 published races",
    );
    fireEvent.change(screen.getByLabelText("Search detailed races"), {
      target: { value: "nothing" },
    });
    expect(
      screen.getByText("No races match your filters."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "13 of 13 published races",
    );
    expect(screen.getAllByRole("link", { name: /Grand Prix / })).toHaveLength(
      13,
    );
  });
  it("ignores a response from an aborted request even if its transport resolves", async () => {
    let resolveFirst!: (response: SessionListResponse) => void;
    vi.mocked(getSessions)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockResolvedValue(response);
    render(
      <StrictMode>
        <SessionBrowser />
      </StrictMode>,
    );
    const signal = vi.mocked(getSessions).mock.calls[0][0]?.signal;
    expect(signal?.aborted).toBe(true);
    await firstRaceLink();
    await act(async () =>
      resolveFirst({
        ...response,
        items: [{ ...sessions[0], event_name: "Stale race" }],
      }),
    );
    await waitFor(() =>
      expect(screen.queryByText("Stale race")).not.toBeInTheDocument(),
    );
  });
});
