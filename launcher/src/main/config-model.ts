import path from "node:path";
import {
  ENVIRONMENT_KEYS,
  type EditableConfig,
  type EnvironmentConfig,
  type EnvironmentKey,
} from "../shared";

export const DEFAULT_IMAGE_HOST_ALLOWLIST =
  "image.tmdb.org,media.themoviedb.org,artworks.thetvdb.com,coverartarchive.org";

export function defaultEnvironment(localAppData: string): EnvironmentConfig {
  return {
    ORCHESTRATOR_HOST: "127.0.0.1",
    ORCHESTRATOR_PORT: "9088",
    ZENSTREAM_PUBLIC_WEB_URL: "",
    METADATA_PATH: path.join(
      localAppData,
      "ZenStream Orchestrator",
      "metadata",
    ),
    CORS_ORIGINS: "",
    TRUSTED_PROXY_IPS: "",
    ZENSTREAM_LOG_LEVEL: "INFO",
    FFMPEG_PATH: "",
    FFPROBE_PATH: "",
    MAX_TRANSCODES: "0",
    MAX_TRANSCODES_PER_USER: "0",
    PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS: "45",
    FOREGROUND_WORKERS: "16",
    CONTROL_WORKERS: "8",
    AUTH_WORKERS: "4",
    CONTROL_QUEUE: "32",
    AUTH_QUEUE: "16",
    METADATA_ROOT_WORKERS: "12",
    METADATA_FETCH_WORKERS: "12",
    METADATA_ASSET_WORKERS: "12",
    METADATA_PROVIDER_TIMEOUT_SECONDS: "20",
    METADATA_IMAGE_TIMEOUT_SECONDS: "20",
    METADATA_IMAGE_HOST_ALLOWLIST: DEFAULT_IMAGE_HOST_ALLOWLIST,
  };
}

const integerBounds: Partial<Record<EnvironmentKey, [number, number]>> = {
  ORCHESTRATOR_PORT: [1, 65_535],
  MAX_TRANSCODES: [0, 64],
  MAX_TRANSCODES_PER_USER: [0, 64],
  FOREGROUND_WORKERS: [2, 32],
  CONTROL_WORKERS: [1, 64],
  AUTH_WORKERS: [1, 16],
  CONTROL_QUEUE: [0, 256],
  AUTH_QUEUE: [0, 128],
  METADATA_ROOT_WORKERS: [1, 64],
  METADATA_FETCH_WORKERS: [1, 64],
  METADATA_ASSET_WORKERS: [1, 64],
};

const decimalBounds: Partial<Record<EnvironmentKey, [number, number]>> = {
  PLAYBACK_SESSION_IDLE_TIMEOUT_SECONDS: [15, 3_600],
  METADATA_PROVIDER_TIMEOUT_SECONDS: [3, 120],
  METADATA_IMAGE_TIMEOUT_SECONDS: [3, 60],
};

export function normalizeEnvironment(
  input: Partial<Record<EnvironmentKey, unknown>>,
  defaults: EnvironmentConfig,
): EnvironmentConfig {
  return Object.fromEntries(
    ENVIRONMENT_KEYS.map((key) => [
      key,
      typeof input[key] === "string" ? input[key].trim() : defaults[key],
    ]),
  ) as EnvironmentConfig;
}

export function validateEnvironment(environment: EnvironmentConfig): string[] {
  const errors: string[] = [];
  if (!environment.ORCHESTRATOR_HOST) errors.push("Server host is required.");
  if (!environment.METADATA_PATH) errors.push("Metadata path is required.");
  if (environment.ZENSTREAM_PUBLIC_WEB_URL) {
    try {
      const parsed = new URL(environment.ZENSTREAM_PUBLIC_WEB_URL);
      if (!/^https?:$/.test(parsed.protocol))
        throw new Error("invalid protocol");
      if (parsed.username || parsed.password || parsed.search || parsed.hash) {
        throw new Error("URL must be an origin");
      }
      if (parsed.pathname !== "/") throw new Error("URL must be an origin");
    } catch {
      errors.push(
        "Public web URL must be a valid HTTP(S) origin, such as https://stream.example.com.",
      );
    }
  }
  if (!environment.METADATA_IMAGE_HOST_ALLOWLIST) {
    errors.push("Metadata image host allowlist cannot be empty.");
  }
  if (
    !["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].includes(
      environment.ZENSTREAM_LOG_LEVEL.toUpperCase(),
    )
  ) {
    errors.push("Log level is invalid.");
  }

  for (const [key, [minimum, maximum]] of Object.entries(integerBounds) as [
    EnvironmentKey,
    [number, number],
  ][]) {
    const value = Number(environment[key]);
    if (!Number.isInteger(value) || value < minimum || value > maximum) {
      errors.push(
        `${key} must be a whole number from ${minimum} to ${maximum}.`,
      );
    }
  }
  for (const [key, [minimum, maximum]] of Object.entries(decimalBounds) as [
    EnvironmentKey,
    [number, number],
  ][]) {
    const value = Number(environment[key]);
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      errors.push(`${key} must be from ${minimum} to ${maximum}.`);
    }
  }

  for (const origin of environment.CORS_ORIGINS.split(",")
    .map((value) => value.trim())
    .filter(Boolean)) {
    try {
      const parsed = new URL(origin);
      if (!parsed.protocol.startsWith("http"))
        throw new Error("invalid protocol");
    } catch {
      errors.push(`CORS origin is not a valid HTTP(S) URL: ${origin}`);
    }
  }
  return errors;
}

export function defaultEditableConfig(localAppData: string): EditableConfig {
  return {
    environment: defaultEnvironment(localAppData),
    secretConfigured: true,
    startWithWindows: false,
  };
}
