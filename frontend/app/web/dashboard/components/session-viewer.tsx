"use client";

import {
	IconDeviceDesktop,
	IconInfoCircle,
	IconPlayerPause,
	IconPlayerPlay,
	IconPlayerStop,
} from "@tabler/icons-react";
import { DashboardModal } from "./dashboard-surface";

export type LiveSession = {
	id: string;
	user: { id: string; username: string };
	device: {
		id?: string | null;
		type?: string | null;
		browser?: string | null;
		operatingSystem?: string | null;
		name?: string | null;
		clientName?: string | null;
		clientVersion?: string | null;
		ipAddress?: string | null;
	};
	item: {
		id: string;
		title: string;
		type?: string | null;
		seasonNumber?: number | null;
		episodeNumber?: number | null;
		subtitle?: string | null;
	};
	playback: {
		mode: string;
		state: string;
		engine?: string | null;
		positionSeconds: number;
		durationSeconds?: number | null;
		paused: boolean;
		workerSessionId?: string | null;
		requestedBitrate?: number | null;
		audioStreamId?: string | null;
		requestedMode?: string | null;
	};
	timestamps: {
		createdAt?: string | null;
		lastHeartbeatAt?: string | null;
		endedAt?: string | null;
	};
};

export type SessionDetail = LiveSession & {
	diagnostics?: {
		sourceId?: string | null;
		container?: string | null;
		resolution?: { width?: number | null; height?: number | null };
		videoCodec?: string | null;
		audioCodec?: string | null;
		audioChannels?: number | null;
		selectedAudioStream?: Record<string, unknown> | null;
		sourceBitrate?: number | null;
		requestedBitrate?: number | null;
		worker?: {
			sessionId?: string | null;
			state?: string | null;
			processAlive?: boolean;
		} | null;
	};
};

export type SessionCommand = "pause" | "resume" | "stop";

function formatTime(value: number | null | undefined) {
	if (!Number.isFinite(value) || value == null) return "0:00";
	const seconds = Math.max(0, Math.floor(value));
	return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

export function formatBitrate(value: number | null | undefined) {
	if (!Number.isFinite(value) || value == null || value <= 0) return "Automatic";
	if (value >= 1_000_000)
		return `${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)} Mbps`;
	return `${Math.round(value / 1_000)} Kbps`;
}

export function playbackModeLabel(value: string | null | undefined) {
	return (
		(
			{
				direct: "Direct play",
				remux: "Remux",
				"audio-transcode": "Audio transcode",
				"video-transcode": "Video transcode",
			} as Record<string, string>
		)[value || ""] ||
		value ||
		"Unknown"
	);
}

function sessionGradient(session: LiveSession) {
	if (session.playback.mode === "video-transcode")
		return "linear-gradient(145deg,#4b160e,#1b1010)";
	if (session.playback.mode === "audio-transcode")
		return "linear-gradient(145deg,#3d2410,#15100d)";
	if (session.playback.mode === "remux")
		return "linear-gradient(145deg,#242538,#11121c)";
	return "linear-gradient(145deg,#260b72,#111125)";
}

export function SessionCard({
	session,
	onCommand,
	onInfo,
	commandState,
}: {
	session: LiveSession;
	onCommand: (command: SessionCommand) => void;
	onInfo: () => void;
	commandState?: string;
}) {
	const duration = session.playback.durationSeconds || 0;
	const position = session.playback.positionSeconds || 0;
	const progress =
		duration > 0 ? Math.min(100, Math.max(0, (position / duration) * 100)) : 0;
	const client =
		session.device.browser || session.device.clientName || "Unknown device";
	return (
		<article
			style={{
				width: 240,
				minWidth: 240,
				height: 207,
				borderRadius: 11,
				overflow: "hidden",
				background: "#090909",
				border: "1px solid rgba(255,255,255,.04)",
				boxShadow: "0 8px 24px rgba(0,0,0,.18)",
			}}
		>
			<div
				style={{
					height: 136,
					padding: "9px 9px 0",
					background: sessionGradient(session),
					position: "relative",
				}}
			>
				<div style={{ display: "flex", alignItems: "center", gap: 7 }}>
					<span
						style={{
							width: 24,
							height: 24,
							display: "grid",
							placeItems: "center",
							borderRadius: 5,
							background: "rgba(0,0,0,.35)",
							color: "#d5d5d5",
						}}
					>
						<IconDeviceDesktop size={14} stroke={1.5} />
					</span>
					<div style={{ minWidth: 0 }}>
						<div
							style={{
								color: "#eee",
								fontSize: 11,
								fontWeight: 600,
								whiteSpace: "nowrap",
								overflow: "hidden",
								textOverflow: "ellipsis",
							}}
						>
							{client}
						</div>
						<div style={{ color: "rgba(255,255,255,.5)", fontSize: 9 }}>
							{session.device.clientVersion || "Unknown version"}
						</div>
					</div>
					{session.playback.paused && (
						<span
							style={{
								marginLeft: "auto",
								background: "#52392e",
								color: "#e7d2c9",
								borderRadius: 4,
								padding: "3px 6px",
								fontSize: 8,
								fontWeight: 700,
							}}
						>
							PAUSED
						</span>
					)}
				</div>
				<div style={{ position: "absolute", left: 9, right: 9, bottom: 9 }}>
					<div
						style={{
							color: "#fff",
							fontSize: 12,
							fontWeight: 600,
							whiteSpace: "nowrap",
							overflow: "hidden",
							textOverflow: "ellipsis",
						}}
					>
						{session.item.title}
					</div>
					<div style={{ color: "rgba(255,255,255,.62)", fontSize: 9, marginTop: 2 }}>
						{session.item.subtitle || playbackModeLabel(session.playback.mode)}
					</div>
				</div>
			</div>
			<div style={{ height: 3, background: "#222" }}>
				<div
					style={{
						width: `${progress}%`,
						height: "100%",
						background: "var(--primary)",
					}}
				/>
			</div>
			<div
				style={{
					height: 68,
					padding: "8px 10px",
					display: "flex",
					flexDirection: "column",
					justifyContent: "space-between",
				}}
			>
				<div
					style={{ display: "flex", alignItems: "center", gap: 8, color: "#777" }}
				>
					<button
						type="button"
						title={session.playback.paused ? "Resume" : "Pause"}
						onClick={() => onCommand(session.playback.paused ? "resume" : "pause")}
						style={{
							border: 0,
							background: "none",
							color: "#777",
							padding: 0,
							cursor: "pointer",
						}}
					>
						{session.playback.paused ? (
							<IconPlayerPlay size={12} />
						) : (
							<IconPlayerPause size={12} />
						)}
					</button>
					<button
						type="button"
						title="Stop"
						onClick={() => onCommand("stop")}
						style={{
							border: 0,
							background: "none",
							color: "#777",
							padding: 0,
							cursor: "pointer",
						}}
					>
						<IconPlayerStop size={12} />
					</button>
					<button
						type="button"
						title="Details"
						onClick={onInfo}
						style={{
							border: 0,
							background: "none",
							color: "#777",
							padding: 0,
							cursor: "pointer",
						}}
					>
						<IconInfoCircle size={13} />
					</button>
					<span
						style={{
							marginLeft: "auto",
							fontFamily: "var(--font-mono)",
							fontSize: 9,
						}}
					>
						{formatTime(position)} / {formatTime(duration)}
					</span>
				</div>
				<div
					style={{
						display: "flex",
						alignItems: "center",
						gap: 5,
						color: "#555",
						fontSize: 10,
					}}
				>
					<span
						style={{
							width: 5,
							height: 5,
							borderRadius: "50%",
							background: "var(--success)",
						}}
					/>
					<span
						style={{
							overflow: "hidden",
							textOverflow: "ellipsis",
							whiteSpace: "nowrap",
						}}
					>
						{session.user.username}
					</span>
					{commandState && (
						<span
							style={{ marginLeft: "auto", color: "var(--warning)", fontSize: 9 }}
						>
							{commandState}
						</span>
					)}
				</div>
			</div>
		</article>
	);
}

function DetailRow({
	label,
	value,
}: {
	label: string;
	value: React.ReactNode;
}) {
	return (
		<div>
			<div style={{ color: "#555", fontSize: 10, marginBottom: 4 }}>{label}</div>
			<div style={{ color: "#ddd", fontSize: 12, wordBreak: "break-word" }}>
				{value || "—"}
			</div>
		</div>
	);
}

export function SessionDetailModal({
	detail,
	open,
	onClose,
	loading,
	error,
}: {
	detail: SessionDetail | null;
	open: boolean;
	onClose: () => void;
	loading?: boolean;
	error?: string;
}) {
	return (
		<DashboardModal open={open} onClose={onClose} title="Playback details">
			{loading && (
				<p style={{ color: "#777", fontSize: 13 }}>Loading live diagnostics…</p>
			)}
			{error && <p style={{ color: "var(--danger)", fontSize: 13 }}>{error}</p>}
			{detail && (
				<div style={{ display: "grid", gap: 18 }}>
					<div
						style={{
							display: "grid",
							gridTemplateColumns: "repeat(2,minmax(0,1fr))",
							gap: 14,
						}}
					>
						<DetailRow label="User" value={detail.user.username} />
						<DetailRow
							label="Device"
							value={detail.device.name || detail.device.type}
						/>
						<DetailRow label="Browser" value={detail.device.browser} />
						<DetailRow
							label="Operating system"
							value={detail.device.operatingSystem}
						/>
						<DetailRow
							label="Client"
							value={`${detail.device.clientName || "Unknown"} ${detail.device.clientVersion || ""}`}
						/>
						<DetailRow label="IP address" value={detail.device.ipAddress} />
					</div>
					<div style={{ height: 1, background: "#1b1b1b" }} />
					<div
						style={{
							display: "grid",
							gridTemplateColumns: "repeat(2,minmax(0,1fr))",
							gap: 14,
						}}
					>
						<DetailRow
							label="Now playing"
							value={`${detail.item.title}${detail.item.subtitle ? ` · ${detail.item.subtitle}` : ""}`}
						/>
						<DetailRow
							label="Playback mode"
							value={playbackModeLabel(detail.playback.mode)}
						/>
						<DetailRow
							label="Position"
							value={`${formatTime(detail.playback.positionSeconds)} / ${formatTime(detail.playback.durationSeconds)}`}
						/>
						<DetailRow
							label="State"
							value={detail.playback.paused ? "Paused" : "Playing"}
						/>
						<DetailRow label="Engine" value={detail.playback.engine} />
						<DetailRow
							label="Last heartbeat"
							value={detail.timestamps.lastHeartbeatAt}
						/>
					</div>
					{detail.diagnostics && (
						<>
							<div style={{ height: 1, background: "#1b1b1b" }} />
							<div
								style={{
									display: "grid",
									gridTemplateColumns: "repeat(2,minmax(0,1fr))",
									gap: 14,
								}}
							>
								<DetailRow label="Container" value={detail.diagnostics.container} />
								<DetailRow
									label="Resolution"
									value={
										detail.diagnostics.resolution?.width &&
										detail.diagnostics.resolution.height
											? `${detail.diagnostics.resolution.width} × ${detail.diagnostics.resolution.height}`
											: undefined
									}
								/>
								<DetailRow label="Video codec" value={detail.diagnostics.videoCodec} />
								<DetailRow label="Audio codec" value={detail.diagnostics.audioCodec} />
								<DetailRow
									label="Audio channels"
									value={
										detail.diagnostics.audioChannels
											? `${detail.diagnostics.audioChannels} channels`
											: undefined
									}
								/>
								<DetailRow
									label="Source bitrate"
									value={formatBitrate(detail.diagnostics.sourceBitrate)}
								/>
								<DetailRow
									label="Requested bitrate cap"
									value={formatBitrate(detail.diagnostics.requestedBitrate)}
								/>
								<DetailRow
									label="HLS worker"
									value={
										detail.diagnostics.worker
											? `${detail.diagnostics.worker.state || "unknown"}${detail.diagnostics.worker.processAlive ? " · alive" : ""}`
											: "Not applicable"
									}
								/>
							</div>
						</>
					)}
				</div>
			)}
		</DashboardModal>
	);
}
