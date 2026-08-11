import { randomBytes } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { app, safeStorage } from "electron";
import type {
  EditableConfig,
  EnvironmentConfig,
  SaveConfigRequest,
} from "../shared";
import {
  defaultEditableConfig,
  defaultEnvironment,
  normalizeEnvironment,
  validateEnvironment,
} from "./config-model";

interface StoredConfig {
  schemaVersion: 1;
  environment: EnvironmentConfig;
  encryptedSecret: string;
  startWithWindows: boolean;
}

export class ConfigStore {
  private stored: StoredConfig | null = null;

  private get localAppData(): string {
    return process.env.LOCALAPPDATA || path.dirname(app.getPath("userData"));
  }

  private get configPath(): string {
    return path.join(app.getPath("userData"), "launcher-config.json");
  }

  private encryptSecret(value: string): string {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("Windows secure storage is unavailable for SECRET_KEY.");
    }
    return safeStorage.encryptString(value).toString("base64");
  }

  private decryptSecret(value: string): string {
    if (!safeStorage.isEncryptionAvailable()) {
      throw new Error("Windows secure storage is unavailable for SECRET_KEY.");
    }
    return safeStorage.decryptString(Buffer.from(value, "base64"));
  }

  async load(): Promise<EditableConfig> {
    const defaults = defaultEnvironment(this.localAppData);
    try {
      const raw = JSON.parse(
        await readFile(this.configPath, "utf8"),
      ) as Partial<StoredConfig>;
      if (typeof raw.encryptedSecret !== "string" || !raw.encryptedSecret) {
        throw new Error("Stored SECRET_KEY is missing.");
      }
      this.decryptSecret(raw.encryptedSecret);
      this.stored = {
        schemaVersion: 1,
        environment: normalizeEnvironment(raw.environment ?? {}, defaults),
        encryptedSecret: raw.encryptedSecret,
        startWithWindows: Boolean(raw.startWithWindows),
      };
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      this.stored = {
        schemaVersion: 1,
        environment: defaults,
        encryptedSecret: this.encryptSecret(randomBytes(32).toString("hex")),
        startWithWindows: false,
      };
      await this.persist();
    }
    return this.view();
  }

  view(): EditableConfig {
    if (!this.stored) throw new Error("Launcher configuration has not loaded.");
    return {
      environment: { ...this.stored.environment },
      secretConfigured: true,
      startWithWindows: this.stored.startWithWindows,
    };
  }

  defaults(): EditableConfig {
    return defaultEditableConfig(this.localAppData);
  }

  environment(): NodeJS.ProcessEnv {
    if (!this.stored) throw new Error("Launcher configuration has not loaded.");
    const environment: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(this.stored.environment)) {
      if (value !== "") environment[key] = value;
    }
    environment.SECRET_KEY = this.decryptSecret(this.stored.encryptedSecret);
    return environment;
  }

  async save(request: SaveConfigRequest): Promise<EditableConfig> {
    if (!this.stored) throw new Error("Launcher configuration has not loaded.");
    const environment = normalizeEnvironment(
      request.environment,
      defaultEnvironment(this.localAppData),
    );
    const errors = validateEnvironment(environment);
    if (errors.length) throw new Error(errors.join("\n"));

    let encryptedSecret = this.stored.encryptedSecret;
    if (request.secret.mode === "generate") {
      encryptedSecret = this.encryptSecret(randomBytes(32).toString("hex"));
    } else if (request.secret.mode === "replace") {
      if (request.secret.value.trim().length < 32) {
        throw new Error("SECRET_KEY must contain at least 32 characters.");
      }
      encryptedSecret = this.encryptSecret(request.secret.value);
    }
    this.stored = {
      schemaVersion: 1,
      environment,
      encryptedSecret,
      startWithWindows: request.startWithWindows,
    };
    await this.persist();
    return this.view();
  }

  private async persist(): Promise<void> {
    if (!this.stored) return;
    const directory = path.dirname(this.configPath);
    const temporary = `${this.configPath}.${process.pid}.tmp`;
    await mkdir(directory, { recursive: true });
    await writeFile(temporary, `${JSON.stringify(this.stored, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    await rename(temporary, this.configPath);
  }
}
