# Developer Guide

## 📜 Using the Swagger API

The ZenStream Orchestrator provides a Swagger API for easy interaction with the backend services. Follow the steps below to access and use the Swagger API:

1. **Access the Swagger UI**:

   - Open your web browser and navigate to `http://localhost:9090/api/swagger/` if running locally.
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
