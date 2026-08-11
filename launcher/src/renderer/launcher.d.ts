import type { LauncherBridge } from "../shared";

declare global {
  interface Window {
    zenstreamLauncher: LauncherBridge;
  }
}

export {};
