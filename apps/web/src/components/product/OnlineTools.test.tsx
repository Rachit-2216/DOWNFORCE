import { StrictMode } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SessionSummary } from "@/lib/api/types";

import { OnlineTools } from "./OnlineTools";

const hungary = {
  session_id:
    "session-2024-event-formula-1-hungarian-grand-prix-2024-sha256-cdc7f18370f678e135dd423e4794e6c76b2a318c08c5b7cf80f158e32ed61f10-type-race",
  dataset_id: "dataset-hungary",
  season: 2024,
  event_name: "FORMULA 1 HUNGARIAN GRAND PRIX 2024",
  session_type: "race",
  provider: "fastf1",
  created_at_utc: "2026-08-07T00:00:00Z",
} satisfies SessionSummary;
const britain = {
  ...hungary,
  session_id:
    "session-2023-event-formula-1-aramco-british-grand-prix-2023-sha256-4d61aa57796468000b205d16df24e22da4b6377c9aeebffe73b2c3dc49597a56-type-race",
  dataset_id: "dataset-britain",
  season: 2023,
  event_name: "FORMULA 1 ARAMCO BRITISH GRAND PRIX 2023",
};
const monza = {
  ...britain,
  session_id:
    "session-2023-event-formula-1-pirelli-gran-premio-ditalia-2023-sha256-0974cff5fa7bbb9f8b54e7c88546e03a4ea7d294f4311782406fc351c08172d3-type-race",
  dataset_id: "dataset-monza",
  event_name: "FORMULA 1 PIRELLI GRAN PREMIO D’ITALIA 2023",
};
const other = {
  ...hungary,
  session_id: "unknown/future?race",
  season: 2027,
  event_name: "Another published race",
};

function response(items: SessionSummary[]) {
  return new Response(
    JSON.stringify({ items, offset: 0, limit: 100, total: items.length }),
    {
      status: 200,
      headers: { "Content-Type": "application/json" },
    },
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("OnlineTools", () => {
  it("lets visitors inspect a tool and opens it in their chosen published race", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response([hungary, britain]),
    );
    render(<OnlineTools />);
    await screen.findAllByRole("article");
    fireEvent.click(screen.getByRole("tab", { name: "Strategy" }));
    fireEvent.keyDown(screen.getByRole("tab", { name: "Strategy" }), {
      key: "ArrowLeft",
    });
    expect(screen.getByRole("tab", { name: "Intelligence" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.keyDown(screen.getByRole("tab", { name: "Intelligence" }), {
      key: "ArrowRight",
    });
    fireEvent.change(screen.getByRole("combobox", { name: "Preview race" }), {
      target: { value: britain.session_id },
    });
    const panel = screen.getByRole("tabpanel");
    expect(
      within(panel).getByRole("link", { name: /Open strategy/ }),
    ).toHaveAttribute(
      "href",
      `/app/replay/${britain.session_id}?mode=strategy&driver=1&lap=5`,
    );
    expect(
      within(panel).getByText(/Simulated, not observed/),
    ).toBeInTheDocument();
  });
  it("fetches once, selects actual published representatives, and exposes all three race modes", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response([other, monza, britain, hungary]));
    render(<OnlineTools />);
    expect(screen.getByRole("status")).toHaveTextContent(
      /loading published races/i,
    );

    const cards = await screen.findAllByRole("article");
    expect(cards).toHaveLength(3);
    for (const [index, race] of [hungary, britain, monza].entries()) {
      expect(within(cards[index]).getByRole("heading")).toHaveTextContent(
        race.event_name,
      );
      expect(
        within(cards[index]).getByText(String(race.season)),
      ).toBeInTheDocument();
      for (const mode of ["replay", "intelligence", "strategy"]) {
        const href = within(cards[index])
          .getByRole("link", { name: new RegExp(`^${mode}`, "i") })
          .getAttribute("href")!;
        const url = new URL(href, "https://downforce.test");
        expect(decodeURIComponent(url.pathname)).toBe(
          `/app/replay/${race.session_id}`,
        );
        expect(url.searchParams.get("mode")).toBe(mode);
        expect(url.searchParams.has("view")).toBe(false);
        if (mode !== "replay") {
          expect(url.searchParams.get("driver")).toBe("1");
          expect(url.searchParams.get("lap")).toBe("5");
        }
      }
    }
    const nav = screen.getByRole("navigation", { name: "Online tools" });
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
    expect(
      within(nav).getByRole("link", { name: /race archive/i }),
    ).toHaveAttribute("href", "/app");
    expect(
      within(nav).getByRole("link", { name: /analytics/i }),
    ).toHaveAttribute("href", "/app/analytics");
    expect(
      screen.getByRole("link", { name: /full replay library/i }),
    ).toHaveAttribute("href", "/app/replays");
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0][0])).toContain(
      "/api/v1/sessions?limit=100",
    );
  });

  it("falls back only to returned races and does not invent context for unfamiliar identities", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response([other]));
    render(<OnlineTools />);
    const card = await screen.findByRole("article");
    expect(within(card).getByRole("heading")).toHaveTextContent(
      other.event_name,
    );
    expect(
      within(card).getByRole("link", { name: /^intelligence/i }),
    ).toHaveAttribute(
      "href",
      "/app/replay/unknown%2Ffuture%3Frace?mode=intelligence",
    );
    expect(screen.queryByText(hungary.event_name)).not.toBeInTheDocument();
  });

  it("shows an honest retry gate, then replaces it with returned races", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(response([hungary]));
    render(<OnlineTools />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not load/i,
    );
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry races" }));
    expect(await screen.findByRole("article")).toHaveTextContent(
      hungary.event_name,
    );
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("keeps archive and library navigation usable when no sessions are published", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response([]));
    render(<OnlineTools />);
    expect(
      await screen.findByText(/no detailed races are published/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /full replay library/i }),
    ).toHaveAttribute("href", "/app/replays");
    expect(screen.getByRole("link", { name: /race archive/i })).toHaveAttribute(
      "href",
      "/app",
    );
  });

  it("aborts its request on unmount even when the response arrives late", async () => {
    let finish!: (value: Response) => void;
    let signal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation((_input, options) => {
      signal = options?.signal as AbortSignal;
      return new Promise((resolve) => {
        finish = resolve;
      });
    });
    const view = render(<OnlineTools />);
    expect(signal?.aborted).toBe(false);
    view.unmount();
    expect(signal?.aborted).toBe(true);
    await act(async () => finish(response([hungary])));
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("ignores stale success from an aborted StrictMode request", async () => {
    let stale!: (value: Response) => void;
    let firstSignal: AbortSignal | undefined;
    vi.spyOn(globalThis, "fetch")
      .mockImplementationOnce((_input, options) => {
        firstSignal = options?.signal as AbortSignal;
        return new Promise((resolve) => {
          stale = resolve;
        });
      })
      .mockResolvedValueOnce(response([hungary]));
    render(
      <StrictMode>
        <OnlineTools />
      </StrictMode>,
    );
    expect(await screen.findByRole("article")).toHaveTextContent(
      hungary.event_name,
    );
    expect(firstSignal?.aborted).toBe(true);
    await act(async () => stale(response([other])));
    expect(screen.getByRole("article")).toHaveTextContent(hungary.event_name);
    expect(screen.queryByText(other.event_name)).not.toBeInTheDocument();
  });
});
