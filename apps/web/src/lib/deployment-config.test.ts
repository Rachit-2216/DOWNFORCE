// @vitest-environment node
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

const root = fileURLToPath(new URL("../../../../", import.meta.url));

describe("production deployment", () => {
  it("keeps private prose files ignored while allowing the product README and dependency inputs", () => {
    const rules = readFileSync(`${root}/.gitignore`, "utf8").split(/\r?\n/);
    expect(rules).toContain("**/*.md");
    expect(rules).toContain("**/*.txt");
    expect(rules).toContain("!/README.md");
    expect(rules).toContain("!**/requirements*.txt");
    expect(rules).not.toContain("!/data/README.md");
    expect(rules).not.toContain("!data/README.md");
  });

  it("deploys both services and routes the API before the frontend catch-all", () => {
    const configPath = `${root}/vercel.json`;
    expect(
      existsSync(configPath),
      "an example file does not configure Vercel",
    ).toBe(true);
    const config = JSON.parse(readFileSync(configPath, "utf8"));
    expect(config.services.frontend.root).toBe("apps/web");
    expect(config.services.backend.root).toBe(".");
    expect(config.services.backend.entrypoint).toBe("apps.api.index:app");
    // Managed Python installation uses locked production dependencies and enables
    // Vercel's standard-size packaging; custom commands disable that path.
    expect(config.services.backend.installCommand).toBeUndefined();
    expect(
      config.services.backend.functions["apps/api/index.py"].includeFiles,
    ).toBe("{product-data/**,packages/downforce_core/src/**}");
    expect(config.rewrites).toEqual([
      { source: "/api/v1/(.*)", destination: { service: "backend" } },
      { source: "/health", destination: { service: "backend" } },
      { source: "/(.*)", destination: { service: "frontend" } },
    ]);
    expect(
      config.services.backend.functions["apps/api/index.py"].excludeFiles,
    ).toContain("**/graphify-out/**");
    expect(
      config.services.backend.functions["apps/api/index.py"].excludeFiles
        .length,
    ).toBeLessThanOrEqual(256);
  });

  it.each([
    [undefined, undefined, "local"],
    ["1", undefined, "visitor"],
    ["1", "local", "local"],
    [undefined, "visitor", "visitor"],
  ])(
    "embeds a shared server/client public profile for VERCEL=%s and override=%s",
    async (vercel, override, expected) => {
      vi.stubEnv("VERCEL", vercel);
      vi.stubEnv("NEXT_PUBLIC_DATA_PROFILE", override);
      const { default: config } = await import("../../next.config");
      expect(config.env?.NEXT_PUBLIC_DATA_PROFILE).toBe(expected);
    },
  );

  it("keeps private local data, credentials and agent artifacts out of deployment uploads", () => {
    const ignorePath = `${root}/.vercelignore`;
    expect(existsSync(ignorePath)).toBe(true);
    const exclusions = readFileSync(ignorePath, "utf8").split(/\r?\n/);
    for (const name of [
      ".downforce/",
      ".env",
      ".env.*",
      ".agents/",
      ".codex/",
      ".aws/",
      "**/graphify-out/",
      "**/*.md",
      "test-results/",
      ".playwright-cli/",
    ])
      expect(exclusions).toContain(name);
  });
});
