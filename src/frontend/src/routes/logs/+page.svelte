<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import type { PageData } from './$types';
	import { revealOnTap } from '$lib/revealOnTap';
	import { scrollIndicator } from '$lib/scrollIndicator';

	let { data }: { data: PageData } = $props();

	const LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

	let total = $derived(data.logs.total);
	let perPage = $derived(data.logs.per_page ?? 100);
	let currentPage = $derived(data.page ?? 1);
	let totalPages = $derived(Math.max(1, Math.ceil(total / perPage)));

	// Local state for the text search so we don't navigate on every keystroke.
	let searchText = $state('');
	$effect(() => {
		searchText = data.q ?? '';
	});

	function navigate(overrides: Record<string, string>) {
		const params = new URLSearchParams($page.url.searchParams);
		for (const [k, v] of Object.entries(overrides)) {
			if (v) params.set(k, v);
			else params.delete(k);
		}
		goto(`?${params}`, { invalidateAll: true });
	}

	function onSelect(key: string, e: Event) {
		navigate({ [key]: (e.target as HTMLSelectElement).value, page: '1' });
	}

	function onDate(key: string, e: Event) {
		navigate({ [key]: (e.target as HTMLInputElement).value, page: '1' });
	}

	function submitSearch(e: Event) {
		e.preventDefault();
		navigate({ q: searchText, page: '1' });
	}

	function levelClass(level: string | null): string {
		switch (level) {
			case 'CRITICAL':
			case 'ERROR':
				return 'text-error-500';
			case 'WARNING':
				return 'text-warning-500';
			case 'DEBUG':
				return 'text-surface-500';
			default:
				return '';
		}
	}

	function formatDate(ts: string | null): string {
		if (!ts) return '';
		const d = new Date(ts);
		if (isNaN(d.getTime())) return ts;
		return new Intl.DateTimeFormat(undefined, {
			timeZone: data.timezone,
			dateStyle: 'short',
			timeStyle: 'medium'
		}).format(d);
	}
</script>

<div class="container mx-auto space-y-4 p-6">
	{#if data.loadError}
		<aside class="alert preset-filled-error-500"><p>{data.loadError}</p></aside>
	{/if}

	<div class="flex flex-wrap items-end gap-4">
		<label class="flex flex-col gap-1 text-sm">
			<span class="font-medium">Level</span>
			<select class="select w-40" value={data.level ?? ''} onchange={(e) => onSelect('level', e)}>
				<option value="">All levels</option>
				{#each LEVELS as lvl}
					<option value={lvl}>{lvl}+</option>
				{/each}
			</select>
		</label>

		<label class="flex flex-col gap-1 text-sm">
			<span class="font-medium">Log file</span>
			<select class="select w-64" value={data.file ?? ''} onchange={(e) => onSelect('file', e)}>
				<option value="">Current (earmark.log)</option>
				{#each data.files as f}
					<option value={f.name}>{f.name} ({Math.round(f.size_bytes / 1024)} KB)</option>
				{/each}
			</select>
		</label>

		<label class="flex flex-col gap-1 text-sm">
			<span class="font-medium">From</span>
			<input
				type="datetime-local"
				class="input w-56"
				value={data.from ?? ''}
				onchange={(e) => onDate('from', e)}
			/>
		</label>

		<label class="flex flex-col gap-1 text-sm">
			<span class="font-medium">To</span>
			<input
				type="datetime-local"
				class="input w-56"
				value={data.to ?? ''}
				onchange={(e) => onDate('to', e)}
			/>
		</label>

		<form class="flex items-end gap-2" onsubmit={submitSearch}>
			<label class="flex flex-col gap-1 text-sm">
				<span class="font-medium">Search</span>
				<input
					type="text"
					class="input w-64"
					placeholder="Message or logger…"
					bind:value={searchText}
				/>
			</label>
			<button type="submit" class="btn btn-sm preset-tonal">Search</button>
		</form>

		<span class="text-surface-500 ml-auto text-sm">{total} entries</span>
	</div>

	<div class="table-wrap">
		<table class="table table-hover" style="table-layout: fixed; width: 100%;">
			<thead>
				<tr>
					<th class="w-[28%] md:w-[15%]">Time</th>
					<th class="w-[18%] md:w-[8%]">Level</th>
					<th class="hidden md:table-cell md:w-[17%]">Logger</th>
					<th class="w-[54%] md:w-[60%]">Message</th>
				</tr>
			</thead>
			<tbody>
				{#each data.logs.data as entry, i (i)}
					<tr>
						<td class="truncate" title={formatDate(entry.timestamp)} use:revealOnTap={formatDate(entry.timestamp)}>{formatDate(entry.timestamp)}</td>
						<td class="font-semibold {levelClass(entry.level)}">{entry.level ?? ''}</td>
						<td
							class="text-surface-500 hidden truncate font-mono text-xs md:table-cell"
							title={entry.name ?? ''}
							use:revealOnTap={entry.name ?? ''}>{entry.name ?? ''}</td
						>
						<td class="message-scroll-cell font-mono text-xs" style="display: flex; align-items: center; gap: 2px;">
							<div class="message-scroll" title={entry.message} use:scrollIndicator use:revealOnTap={entry.message}>{entry.message}</div>
						</td>
					</tr>
				{:else}
					<tr>
						<td colspan="4" class="text-surface-500 text-center">No log entries.</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>

	{#if totalPages > 1}
		<div class="flex items-center justify-end gap-3">
			<button
				class="btn btn-sm preset-tonal"
				disabled={currentPage <= 1}
				onclick={() => navigate({ page: String(currentPage - 1) })}
			>
				Previous
			</button>
			<span class="text-surface-500 text-sm">Page {currentPage} of {totalPages}</span>
			<button
				class="btn btn-sm preset-tonal"
				disabled={currentPage >= totalPages}
				onclick={() => navigate({ page: String(currentPage + 1) })}
			>
				Next
			</button>
		</div>
	{/if}
</div>

<style>
	.message-scroll {
		flex: 1;
		min-width: 0;
		overflow-x: scroll;
		white-space: nowrap;
		scrollbar-width: thin;
		scrollbar-color: rgba(128, 128, 128, 0.6) rgba(128, 128, 128, 0.15);
	}
</style>
