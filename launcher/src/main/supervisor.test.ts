import { describe, expect, it } from "vitest";
import {
  buildBackendEnvironment,
  crashRetryDelay,
  dashboardUrlFor,
  launcherLogLine,
} from "./supervisor";

describe("backend supervisor helpers", () => {
  it("opens wildcard hosts through loopback", () => {
    expect(
      dashboardUrlFor({
        ORCHESTRATOR_HOST: "0.0.0.0",
        ORCHESTRATOR_PORT: "9088",
      }),
    ).toBe("http://127.0.0.1:9088/web/");
  });

  it("removes inherited managed values before applying saved configuration", () => {
    const environment = buildBackendEnvironment(
      {
        SECRET_KEY: "old",
        FFMPEG_PATH: "old.exe",
        CONTROL_WORKERS: "old",
        PATH: "system",
      },
      { SECRET_KEY: "new", FFMPEG_PATH: "", CONTROL_WORKERS: "" },
    );
    expect(environment.SECRET_KEY).toBe("new");
    expect(environment.FFMPEG_PATH).toBeUndefined();
    expect(environment.CONTROL_WORKERS).toBeUndefined();
    expect(environment.PATH).toBe("system");
    expect(environment.PYTHONUNBUFFERED).toBe("1");
  });

  it("uses three bounded crash retries", () => {
    expect([0, 1, 2, 3].map(crashRetryDelay)).toEqual([
      1_000,
      3_000,
      10_000,
      null,
    ]);
  });

  it("keeps one-time bootstrap credentials out of launcher logs", () => {
    expect(launcherLogLine("Username: root-admin")).toBe(
      "Username: <redacted>",
    );
    expect(launcherLogLine("Password: one-time-secret")).toBe(
      "Password: <redacted>",
    );
  });
});
