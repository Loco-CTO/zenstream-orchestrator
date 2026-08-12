# Developer Guide

## Windows launcher development

The Electron launcher lives in `launcher/`. It supervises a single native
Orchestrator process; the FastAPI process serves both the API and the exported
administrator dashboard.

```powershell
cd launcher
npm ci
npm test
npm run build
```

For interactive Electron development, set `ZENSTREAM_PYTHON` to a Python
executable with `requirements.txt` installed, build `frontend/` once, and run
`npm run dev`. Use `scripts/build-windows.ps1` for the complete x64
source/virtual-environment and installer pipeline. Do not commit generated files
beneath `assets/ffmpeg/windows`,
`dist/`, `.build/`, or `launcher/release/`.
Windows packaging is serialized by `scripts/pack-windows.ps1`; never bypass its
lock or publish artifacts unless `scripts/validate-electron-package.mjs` passes.

## Metadata artwork conversion

Artwork selection follows provider-native TMDB/TVDB order after configured
language tiers and provider priority. Only the selected winner and one native
order fallback are materialized per locale/category. SVG artwork is rasterized
with `resvg_py==0.3.4`, then encoded as quality-85 WebP using compression level
5. WebP and BlurHash FFmpeg calls are non-interactive and run through the
existing `METADATA_ASSET_WORKERS` pool; there is no separate conversion-worker
setting or FFmpeg thread cap.

## 📜 Using the Swagger API

The ZenStream Orchestrator provides a Swagger API for easy interaction with the backend services. Follow the steps below to access and use the Swagger API:

1. **Access the Swagger UI**:

   - Open your web browser and navigate to `http://localhost:9090/api/docs/` if running locally. This redirects to the generated Swagger UI at `/api/swagger/`.
   - You will see the Swagger UI with all the available endpoints and their descriptions.

2. **Explore Endpoints**:
   - Browse through the available endpoints and their descriptions.
   - Click on an endpoint to expand its details and see the available HTTP methods (GET, POST, etc.).
   - Fill in the required parameters and click "Execute" to send a sample request.

## 🎟️ Authorization with token

To ensure secure access to the API, a token is required for authentication. The token is passed in the request header as `TOKEN: your_token`.

## 🔑 Authorization with API-Key

To ensure secure access to the API, an API key is required for authentication. The API key is passed in the request header as `TOKEN`. Follow the steps below to authorize with an API key:

1. **Create an API Key**:

   - Go to the dashboard.
   - Navigate to `Settings` and press `Create API Key` then copy the newly generated API Key.

2. **Authorize with API Key**:
   - This process is the same with using a `token`, pass the API Key in the request header as `TOKEN: your_api_key`.
