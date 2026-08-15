// Shared API/DTO types. Network calls live in the SvelteKit server routes
// (+page.server.ts / +server.ts); this module only exports the shapes they use.

export interface DocumentSummary {
	document: string;
	title: string | null;
}

export interface ProgressItem {
	id: number;
	document: string;
	progress: string;
	percentage: number;
	device: string;
	device_id: string;
	timestamp: number;
	filename: string | null;
	title: string | null;
	authors: string | null;
	is_latest: boolean | null;
	abs_synced: boolean | null;
	abs_synced_at: number | null;
	abs_synced_position_seconds: number | null;
	abs_sync_error: string | null;
}

export interface ProgressList {
	data: ProgressItem[];
	total: number;
	page: number;
	per_page: number;
}

export interface AbsItemSummary {
	abs_item_id: string;
	title: string;
	author: string | null;
}

export interface EbookFileSummary {
	path: string;
	filename: string;
	title: string | null;
	author: string | null;
}

export type EbookSource = 'local' | 'calibre';

export interface EbookCandidate {
	ref: string;
	title: string;
	author: string | null;
	format: string;
}

export interface MappingRead {
	id: number;
	user_id: number;
	abs_item_id: string;
	abs_title: string;
	abs_author: string | null;
	ebook_source: EbookSource;
	ebook_path: string | null;
	ebook_filename: string | null;
	ebook_source_ref: string | null;
	kosync_document: string | null;
	created_at: string;
	alignment_job_id: number | null;
	sync_status: string | null;
	sync_progress: number | null;
	sync_error: string | null;
	sync_warnings: string[];
	cache_intact: boolean | null;
	reading_percentage: number | null;
}

export type SortBy = 'title' | 'percentage' | 'progress' | 'device' | 'is_latest' | 'updated_at';
export type SortDir = 'asc' | 'desc';

export interface AppConfig {
	timezone: string;
}

export interface AppSetting {
	key: string;
	label: string;
	description: string;
	value_type: string;
	is_secret: boolean;
	has_db_value: boolean;
	display_value: string;
}

export interface LogEntry {
	timestamp: string | null;
	level: string | null;
	name: string | null;
	message: string;
}

export interface LogList {
	data: LogEntry[];
	total: number;
	page: number;
	per_page: number;
}

export interface LogFileInfo {
	name: string;
	size_bytes: number;
	modified_at: string;
}
