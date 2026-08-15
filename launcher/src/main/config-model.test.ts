import { describe, expect, it } from "vitest";
import {
  defaultEnvironment,
  normalizeEnvironment,
  validateEnvironment,
} from "./config-model";

describe("launcher environment configuration", () => {
  it("uses local-only defaults with persistent Windows metadata", () => {
    const values = defaultEnvironment("C:\\Users\\Alice\\AppData\\Local");
    expect(values.ORCHESTRATOR_HOST).toBe("127.0.0.1");
    expect(values.ORCHESTRATOR_PORT).toBe("9088");
    expect(values.ZENSTREAM_PUBLIC_WEB_URL).toBe("");
    expect(values.METADATA_PATH).toContain("ZenStream Orchestrator");
    expect(values.MAX_TRANSCODES).toBe("0");
  });

  it("merges missing stored values with current defaults", () => {
    const defaults = defaultEnvironment("C:\\Data");
    const normalized = normalizeEnvironment(
      { ORCHESTRATOR_PORT: " 9191 " },
      defaults,
    );
    expect(normalized.ORCHESTRATOR_PORT).toBe("9191");
    expect(normalized.FOREGROUND_WORKERS).toBe("16");
  });

  it("rejects unsafe numeric and origin values", () => {
    const values = defaultEnvironment("C:\\Data");
    values.ORCHESTRATOR_PORT = "70000";
    values.CORS_ORIGINS = "file:///tmp";
    expect(validateEnvironment(values)).toEqual(
      expect.arrayContaining([
        expect.stringContaining("ORCHESTRATOR_PORT"),
        expect.stringContaining("CORS origin"),
      ]),
    );
  });

  it("accepts a public web origin and rejects a URL with a path", () => {
    const values = defaultEnvironment("C:\\Data");
    values.ZENSTREAM_PUBLIC_WEB_URL = "https://stream.example.com";
    expect(validateEnvironment(values)).toEqual([]);
    values.ZENSTREAM_PUBLIC_WEB_URL = "https://stream.example.com/register";
    expect(validateEnvironment(values)).toEqual(
      expect.arrayContaining([expect.stringContaining("Public web URL")]),
    );
  });
});
