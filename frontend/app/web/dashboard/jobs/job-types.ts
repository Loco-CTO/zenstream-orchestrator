export type ProgressDetail = {
	phase?: string | null;
	label?: string | null;
	item?: string | null;
	current?: number | null;
	total?: number | null;
	unit?: string | null;
};

export type Run = {
	id: string;
	kind: string;
	state: string;
	message?: string | null;
	progressDetail?: ProgressDetail | null;
	error?: string | null;
	createdAt: string;
	startedAt?: string | null;
	finishedAt?: string | null;
	progressCurrent: number;
	progressTotal: number;
};

export type JobTrigger =
	| {
			id: string;
			type: "interval";
			intervalSeconds: number;
			options?: Record<string, unknown>;
			nextRunAt?: string | null;
	  }
	| {
			id: string;
			type: "daily";
			time: string;
			options?: Record<string, unknown>;
			nextRunAt?: string | null;
	  }
	| {
			id: string;
			type: "weekly";
			weekday: number;
			time: string;
			options?: Record<string, unknown>;
			nextRunAt?: string | null;
	  }
	| {
			id: string;
			type: "startup";
			options?: Record<string, unknown>;
			nextRunAt?: string | null;
	  };

export type JobOptionDefinition = {
	key: string;
	label: string;
	type: "boolean";
	default?: boolean;
	description?: string;
};

export type Job = {
	id: string;
	key: string;
	name: string;
	description?: string | null;
	kind: string;
	nextRunAt?: string | null;
	lastRunAt?: string | null;
	lastState: string;
	lastMessage?: string | null;
	config?: Record<string, unknown>;
	triggers: JobTrigger[];
	optionDefinitions?: JobOptionDefinition[];
	/** @deprecated legacy fields are omitted by the API */
	intervalMinutes?: number;
	enabled?: boolean;
	recentRuns: Run[];
	historyOnly: boolean;
};

export const activeStates = new Set(["queued", "running", "terminating"]);

export function stateLabel(state?: string | null) {
	return state ? state.replace(/_/g, " ") : "idle";
}

export function progressDetailText(detail?: ProgressDetail | null) {
	if (!detail) return "";
	const parts: string[] = [];
	if (detail.label) parts.push(detail.label);
	if (detail.item) parts.push(detail.item);
	if (
		typeof detail.current === "number" &&
		typeof detail.total === "number" &&
		detail.total > 0
	) {
		parts.push(
			`${detail.current}/${detail.total}${detail.unit ? ` ${detail.unit}` : ""}`,
		);
	}
	return parts.join(" · ");
}

export const stateColor: Record<string, string> = {
	completed: "text-[#5ee3d8]",
	completed_with_warnings: "text-[#f0bf6a]",
	running: "text-[#60b4e8]",
	terminating: "text-[#f0bf6a]",
	terminated: "text-[#8ca19f]",
	queued: "text-[#60b4e8]",
	failed: "text-[#f07070]",
	error: "text-[#f07070]",
	idle: "console-muted",
};
