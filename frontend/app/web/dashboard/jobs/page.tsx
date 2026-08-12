"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
	IconChevronRight,
	IconClock,
	IconPlayerPlay,
	IconRefresh,
} from "@tabler/icons-react";
import { adminFetch, readSession, Session } from "../components/admin-client";
import {
	EmptyState,
	PageHeader,
	StatusMessage,
	SurfaceCard,
} from "../components/dashboard-surface";
import { Job, stateColor } from "./job-types";

export default function JobsPage() {
	const router = useRouter();
	const params = useSearchParams();
	const [session, setSession] = useState<Session | null>(null);
	const [jobs, setJobs] = useState<Job[]>([]);
	const [loading, setLoading] = useState(true);
	const [message, setMessage] = useState("");

	async function load(current = session) {
		if (!current) return;
		setLoading(true);
		const response = await adminFetch("/api/admin/jobs", current);
		if (response.ok)
			setJobs(((await response.json()) as { jobs?: Job[] }).jobs || []);
		else setMessage("Scheduled tasks could not be loaded.");
		setLoading(false);
	}
	async function runNow(job: Job) {
		if (!session) return;
		const response = await adminFetch(`/api/admin/jobs/${job.id}/run`, session, {
			method: "POST",
		});
		setMessage(
			response.ok ? "Task queued in the background." : "Could not queue task.",
		);
		await load(session);
	}
	useEffect(() => {
		const legacyJobId = params.get("jobId");
		if (legacyJobId) {
			router.replace(
				`/web/dashboard/jobs/detail?jobId=${encodeURIComponent(legacyJobId)}`,
			);
			return;
		}
		const current = readSession();
		setSession(current);
		if (current) void load(current);
	}, [params, router]);

	return (
		<div className="dashboard-page">
			<PageHeader
				title="Tasks"
				description="Review scheduled work, tune its cadence, and manage active runs."
				actions={
					<button
						onClick={() => void load()}
						className="material-icon-button"
						aria-label="Refresh tasks"
					>
						<IconRefresh size={17} />
					</button>
				}
			/>
			{message && <StatusMessage>{message}</StatusMessage>}
			<SurfaceCard className="mt-7 overflow-hidden">
				<div className="border-b console-divider px-5 py-4 text-xs uppercase tracking-[.16em] console-muted">
					All tasks{" "}
					<span className="ml-2 normal-case tracking-normal">{jobs.length}</span>
				</div>
				{loading ? (
					<div className="p-8 text-sm console-muted">Loading tasks…</div>
				) : (
					jobs.map((job) => (
						<div
							key={job.id}
							className="flex items-center gap-4 border-b console-divider px-5 py-4 last:border-0 hover:bg-white/[.035]"
						>
							<span className="rounded-full border border-[#5ee3d8]/25 bg-[#5ee3d8]/[.08] p-2 text-[#5ee3d8]">
								<IconClock size={16} />
							</span>
							<Link
								href={`/web/dashboard/jobs/detail?jobId=${encodeURIComponent(job.id)}`}
								className="min-w-0 flex-1 text-left"
							>
								<span className="block truncate text-sm font-medium">{job.name}</span>
								<span className="mt-1 block truncate text-xs console-muted">
									{job.description || job.kind}
								</span>
							</Link>
							<span
								className={`hidden text-xs capitalize sm:block ${stateColor[job.lastState] || "console-muted"}`}
							>
								{job.enabled ? job.lastState : "paused"}
							</span>
							<button
								onClick={() => void runNow(job)}
								className="material-icon-button !h-9 !min-h-9 !w-9 !min-w-9"
								aria-label={`Run ${job.name} now`}
							>
								<IconPlayerPlay size={15} />
							</button>
							<Link
								href={`/web/dashboard/jobs/detail?jobId=${encodeURIComponent(job.id)}`}
								aria-label={`Open ${job.name}`}
								className="console-muted hover:text-white"
							>
								<IconChevronRight size={16} />
							</Link>
						</div>
					))
				)}
				{!loading && !jobs.length && (
					<EmptyState>No scheduled tasks are configured.</EmptyState>
				)}
			</SurfaceCard>
		</div>
	);
}
