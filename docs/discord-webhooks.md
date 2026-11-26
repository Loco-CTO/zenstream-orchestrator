# Discord webhooks

Open **Webhooks** in the Orchestrator dashboard. In Discord, create an incoming webhook in the destination channel’s **Integrations** settings and copy its URL. Add a name and paste the URL into ZenStream. Select any events, use **Select all** to subscribe to every listed event, or save with an empty selection. Use **Send test** to check the destination even when no events are selected. The result appears under the webhook’s name.

Discord channel members can see the selected activity, including usernames and media details. Choose the destination channel accordingly. ZenStream encrypts the webhook URL using the server secret and keeps it out of dashboard responses. Paste the URL again to replace it or after changing the server secret.

Each event produces its own message. Video progress is sampled from online viewer sessions and posts the highest newly observed milestone at 25%, 50%, or 75%. A separate event reports when playback reaches 90%. Seeking can skip milestones, and manual watched changes and offline playback are outside the live video event stream. ZenStream respects the account’s watch history setting for video activity and track starts. Cached artwork is attached when available, so Discord can display it even when the Orchestrator is on a private network.

The dashboard shows the latest delivery result. The sender spaces requests for each destination, respects Discord rate limits, and retries temporary failures. It holds up to 1,000 pending messages per destination and discards undelivered messages after 24 hours. If Discord reports that a webhook is inaccessible, update the URL or use **Send test** after restoring it.
