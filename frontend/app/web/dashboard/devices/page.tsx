"use client";

import { useEffect, useState } from "react";
import { IconDeviceDesktop, IconRefresh, IconTrash } from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	ConfirmDialog,
	EmptyState,
	PageHeader,
} from "../components/dashboard-surface";

type Device = {
	id: string;
	user: { id: string; username: string };
	type?: string | null;
	browser?: string | null;
	operatingSystem?: string | null;
	name?: string | null;
	clientName?: string | null;
	clientVersion?: string | null;
	ipAddress?: string | null;
	firstSeenAt?: string | null;
	lastActiveAt?: string | null;
	active: boolean;
	nowPlaying?: {
		title?: string | null;
		positionSeconds?: number;
		durationSeconds?: number | null;
		paused?: boolean;
	} | null;
};

type User = { id: string; username: string };

function dateLabel(value?: string | null) {
	if (!value) return "Unknown";
	const date = new Date(value);
	return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export default function DevicesPage() {
	const [session, setSession] = useState<Session | null>(null);
	const [devices, setDevices] = useState<Device[]>([]);
	const [users, setUsers] = useState<User[]>([]);
	const [userId, setUserId] = useState("");
	const [error, setError] = useState("");
	const [deviceToRemove, setDeviceToRemove] = useState<Device | null>(null);
	const [busy, setBusy] = useState(false);

	async function load(current = session, filter = userId) {
		if (!current) return;
		const query = filter ? `?userId=${encodeURIComponent(filter)}` : "";
		try {
			const [deviceResponse, userResponse] = await Promise.all([
				adminFetch(`/api/admin/devices${query}`, current),
				adminFetch("/api/admin/users", current),
			]);
			if (!deviceResponse.ok) throw new Error("Devices could not be loaded.");
			setDevices(
				((await deviceResponse.json()) as { devices?: Device[] }).devices || [],
			);
			if (userResponse.ok)
				setUsers(((await userResponse.json()) as { users?: User[] }).users || []);
			setError("");
		} catch (caught) {
			setError(
				caught instanceof Error ? caught.message : "Devices could not be loaded.",
			);
		}
	}

	useEffect(() => {
		const current = readSession();
		setSession(current);
		if (current) void load(current, "");
		// Initial authentication is intentionally read once; filter changes call load explicitly.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, []);

	async function removeDevice() {
		if (!session || !deviceToRemove) return;
		setBusy(true);
		try {
			const response = await adminFetch(
				`/api/admin/devices/${encodeURIComponent(deviceToRemove.id)}`,
				session,
				{ method: "DELETE" },
			);
			if (!response.ok) throw new Error("Device could not be removed.");
			setDeviceToRemove(null);
			await load(session, userId);
		} catch (caught) {
			setError(
				caught instanceof Error ? caught.message : "Device could not be removed.",
			);
		} finally {
			setBusy(false);
		}
	}

	return (
		<div className="dashboard-page dashboard-design">
			<ConfirmDialog
				open={Boolean(deviceToRemove)}
				title="Remove device?"
				description={`Remove ${deviceToRemove?.name || deviceToRemove?.clientName || "this device"}. Its login sessions will be revoked and active playback will be stopped.`}
				confirmLabel="Remove device"
				destructive
				busy={busy}
				onClose={() => !busy && setDeviceToRemove(null)}
				onConfirm={() => void removeDevice()}
			/>
			<PageHeader
				title="Devices"
				description="Every browser and install that has logged in to this server."
				actions={
					<button
						type="button"
						onClick={() => void load()}
						className="material-icon-button"
						title="Refresh devices"
						aria-label="Refresh devices"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{error && (
				<p
					role="alert"
					style={{ color: "var(--danger)", fontSize: 12, marginBottom: 18 }}
				>
					{error}
				</p>
			)}
			<div
				style={{
					display: "flex",
					alignItems: "center",
					justifyContent: "space-between",
					gap: 12,
					marginBottom: 20,
					flexWrap: "wrap",
				}}
			>
				<label
					style={{
						display: "flex",
						alignItems: "center",
						gap: 10,
						color: "#777",
						fontSize: 12,
					}}
				>
					User
					<select
						value={userId}
						onChange={(event) => {
							setUserId(event.target.value);
							void load(session, event.target.value);
						}}
						className="console-input h-10 rounded-xl px-3 text-sm"
					>
						<option value="">All users</option>
						{users.map((user) => (
							<option key={user.id} value={user.id}>
								{user.username}
							</option>
						))}
					</select>
				</label>
				<span style={{ color: "#555", fontSize: 11 }}>
					{devices.length} device{devices.length === 1 ? "" : "s"}
				</span>
			</div>
			{devices.length ? (
				<div
					style={{
						display: "grid",
						gridTemplateColumns: "repeat(auto-fill,minmax(260px,1fr))",
						gap: 12,
					}}
				>
					{devices.map((device) => (
						<article
							key={device.id}
							style={{
								border: "1px solid #191919",
								borderRadius: 11,
								overflow: "hidden",
								background: "#0c0c0c",
							}}
						>
							<div
								style={{
									height: 4,
									background: device.active ? "var(--success)" : "#252525",
								}}
							/>
							<div style={{ padding: 16 }}>
								<div style={{ display: "flex", alignItems: "flex-start", gap: 11 }}>
									<div
										style={{
											width: 34,
											height: 34,
											display: "grid",
											placeItems: "center",
											borderRadius: 8,
											background: "#161616",
											color: "#999",
										}}
									>
										<IconDeviceDesktop size={18} stroke={1.5} />
									</div>
									<div style={{ minWidth: 0, flex: 1 }}>
										<div
											style={{
												color: "#eee",
												fontSize: 13,
												fontWeight: 600,
												whiteSpace: "nowrap",
												overflow: "hidden",
												textOverflow: "ellipsis",
											}}
										>
											{device.name || device.clientName || "Unknown device"}
										</div>
										<div style={{ color: "#666", fontSize: 11, marginTop: 3 }}>
											{device.browser || device.type || "Unknown"} ·{" "}
											{device.clientVersion || "Unknown version"}
										</div>
									</div>
									<span
										title={device.active ? "Active" : "Inactive"}
										style={{
											width: 7,
											height: 7,
											marginTop: 5,
											borderRadius: "50%",
											background: device.active ? "var(--success)" : "#444",
										}}
									/>
								</div>
								{device.nowPlaying && (
									<div
										style={{
											marginTop: 16,
											padding: "10px 11px",
											borderRadius: 8,
											background: "#111",
											color: "#ccc",
											fontSize: 11,
										}}
									>
										<div
											style={{
												color: "#555",
												fontSize: 9,
												textTransform: "uppercase",
												letterSpacing: ".08em",
												marginBottom: 5,
											}}
										>
											Now playing
										</div>
										{device.nowPlaying.title || "Unknown item"}
										{device.nowPlaying.paused ? (
											<span style={{ color: "var(--warning)", marginLeft: 6 }}>
												Paused
											</span>
										) : null}
									</div>
								)}
								<div
									style={{
										display: "grid",
										gridTemplateColumns: "1fr 1fr",
										gap: 13,
										marginTop: 17,
									}}
								>
									<div>
										<div style={{ color: "#555", fontSize: 10 }}>User</div>
										<div style={{ color: "#bbb", fontSize: 11, marginTop: 4 }}>
											{device.user.username}
										</div>
									</div>
									<div>
										<div style={{ color: "#555", fontSize: 10 }}>Last active</div>
										<div style={{ color: "#bbb", fontSize: 11, marginTop: 4 }}>
											{dateLabel(device.lastActiveAt)}
										</div>
									</div>
									<div>
										<div style={{ color: "#555", fontSize: 10 }}>IP address</div>
										<div
											style={{
												color: "#bbb",
												fontSize: 11,
												marginTop: 4,
												fontFamily: "var(--font-mono)",
											}}
										>
											{device.ipAddress || "Unknown"}
										</div>
									</div>
									<div>
										<div style={{ color: "#555", fontSize: 10 }}>OS</div>
										<div style={{ color: "#bbb", fontSize: 11, marginTop: 4 }}>
											{device.operatingSystem || "Unknown"}
										</div>
									</div>
								</div>
								<button
									type="button"
									onClick={() => setDeviceToRemove(device)}
									style={{
										width: "100%",
										marginTop: 18,
										height: 32,
										border: "1px solid #252525",
										borderRadius: 7,
										background: "transparent",
										color: "#777",
										cursor: "pointer",
										display: "flex",
										alignItems: "center",
										justifyContent: "center",
										gap: 6,
										fontSize: 11,
									}}
								>
									<IconTrash size={13} /> Remove
								</button>
							</div>
						</article>
					))}
				</div>
			) : (
				<EmptyState>No devices have logged in yet.</EmptyState>
			)}
		</div>
	);
}
