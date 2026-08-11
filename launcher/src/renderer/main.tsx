import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { createDevelopmentBridge } from "./development-bridge";
import "./styles.css";

if (import.meta.env.DEV && !window.zenstreamLauncher) {
  window.zenstreamLauncher = createDevelopmentBridge();
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
