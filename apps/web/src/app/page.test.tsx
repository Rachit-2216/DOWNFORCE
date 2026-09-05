import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import LandingPage from "./page";

describe("landing page", () => {
  it("renders the production product story and workspace entry", () => {
    render(<LandingPage />);

    expect(
      screen.getByRole("heading", {
        name: "DOWNFORCE",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("link", { name: /enter workspace/i })[0],
    ).toHaveAttribute("href", "/app");
    expect(
      screen.getByRole("heading", {
        name: /see only what\s+was knowable then/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: /compare the decision\.\s+not the hindsight/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", {
        name: /when the model\s+doesn’t know,\s+it says so/i,
      }),
    ).toBeInTheDocument();
  });
});
