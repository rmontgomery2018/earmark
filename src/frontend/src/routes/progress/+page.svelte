<script lang="ts">
	import { goto } from '$app/navigation';
	import { enhance } from '$app/forms';
	import { page } from '$app/stores';
	import Modal from '$lib/Modal.svelte';
	import type { PageData } from './$types';
	import type { SortBy, SortDir, ProgressItem } from '$lib/api';
	import { revealOnTap } from '$lib/revealOnTap';
	import { scrollIndicator } from '$lib/scrollIndicator';

	let { data }: { data: PageData } = $props();

	let items = $state<ProgressItem[]>([]);
	let total = $state<number>(0);
	let perPage = $state<number>(50);
	let currentPage = $state<number>(1);

	let totalPages = $derived(Math.max(1, Math.ceil(total / perPage)));

	let selectedDocument = $state<string>('');
	let sortBy = $state<SortBy>('updated_at');
	let sortDir = $state<SortDir>('desc');

	let pendingDeleteIds = $state<number[]>([]);
	let deleteError = $state<string | null>(null);

	let selectedIds = $state<number[]>([]);

	let allSelected = $derived(items.length > 0 && items.every((i) => selectedIds.includes(i.id)));
	let someSelected = $derived(selectedIds.length > 0 && !allSelected);

	function toggleRow(id: number) {
		selectedIds = selectedIds.includes(id)
			? selectedIds.filter((x) => x !== id)
			: [...selectedIds, id];
	}

	function toggleAll() {
		selectedIds = allSelected ? [] : items.map((i) => i.id);
	}

	const columns: { key: SortBy | null; label: string; thClass: string }[] = [
		{ key: 'title',      label: 'Title',      thClass: 'w-[40%] lg:w-[18%]' },
		{ key: null,         label: 'Document',   thClass: 'hidden lg:table-cell lg:w-[16%]' },
		{ key: 'percentage', label: 'Percentage', thClass: 'w-[12%] lg:w-[8%]' },
		{ key: 'progress',   label: 'Progress',   thClass: 'hidden lg:table-cell lg:w-[13%]' },
		{ key: 'device',     label: 'Device',     thClass: 'hidden lg:table-cell lg:w-[10%]' },
		{ key: 'is_latest',  label: 'Latest',     thClass: 'hidden lg:table-cell lg:w-[6%]' },
		{ key: 'updated_at', label: 'Updated',    thClass: 'w-[25%] lg:w-[13%]' },
		{ key: null,         label: 'ABS Sync',   thClass: 'w-[10%] lg:w-[8%]' },
		{ key: null,         label: 'Actions',    thClass: 'w-[13%] lg:w-[8%]' },
	];

	function navigate(overrides: Record<string, string>) {
		const params = new URLSearchParams($page.url.searchParams);
		for (const [k, v] of Object.entries(overrides)) {
			if (v) params.set(k, v);
			else params.delete(k);
		}
		goto(`?${params}`, { invalidateAll: true });
	}

	function handleDocumentChange(e: Event) {
		const val = (e.target as HTMLSelectElement).value;
		selectedDocument = val;
		navigate({ document: val, page: '1' });
	}

	function handleSort(key: SortBy | null) {
		if (!key) return;
		const newDir: SortDir = sortBy === key && sortDir === 'desc' ? 'asc' : 'desc';
		sortBy = key;
		sortDir = newDir;
		navigate({ sort_by: key, sort_dir: newDir, page: '1' });
	}

	function formatPercent(p: number) {
		return `${(p * 100).toFixed(1)}%`;
	}

	function formatDate(ts: number) {
		return new Intl.DateTimeFormat(undefined, {
			timeZone: data.timezone,
			dateStyle: 'medium',
			timeStyle: 'short',
		}).format(new Date(ts * 1000));
	}

	function formatHms(seconds: number) {
		const total = Math.round(seconds);
		const hours = Math.floor(total / 3600);
		const minutes = Math.floor((total % 3600) / 60);
		const secs = total % 60;
		return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
	}

	function absSyncedTooltip(item: ProgressItem) {
		const lines = ['Synced to ABS'];
		if (item.abs_synced_at) lines.push(formatDate(item.abs_synced_at));
		if (item.abs_synced_position_seconds != null) {
			const pos = item.abs_synced_position_seconds;
			lines.push(`ABS position: ${Math.round(pos)}s (${formatHms(pos)})`);
		}
		return lines.join('\n');
	}


	$effect(() => {
		items = data.progressList.data;
		total = data.progressList.total;
		perPage = data.progressList.per_page ?? 50;
		currentPage = data.page ?? 1;
		selectedDocument = data.document ?? '';
		sortBy = data.sort_by as SortBy;
		sortDir = data.sort_dir as SortDir;
		selectedIds = [];
	});
</script>

<div class="container mx-auto p-6 space-y-4">
	{#if data.loadError}
		<aside class="alert preset-filled-error-500"><p>{data.loadError}</p></aside>
	{/if}
	<div class="flex flex-col gap-4 sm:flex-row sm:items-center">
		<div class="flex items-center gap-2">
			<label for="doc-select" class="text-sm font-medium shrink-0">Filter by title</label>
			<select
				id="doc-select"
				class="select w-full sm:w-72"
				bind:value={selectedDocument}
				onchange={handleDocumentChange}
			>
				<option value="">All documents</option>
				{#each data.documents as doc}
					<option value={doc.document}>{doc.title ?? doc.document}</option>
				{/each}
			</select>
		</div>
		<span class="text-surface-500 text-sm">{total} entries</span>
		{#if selectedIds.length > 0}
			<div class="flex items-center gap-3">
				<span class="text-surface-500 text-sm">{selectedIds.length} selected</span>
				<button
					class="btn btn-sm preset-filled-error-500"
					onclick={() => { pendingDeleteIds = selectedIds; deleteError = null; }}
				>
					Delete selected
				</button>
			</div>
		{/if}
		{#if data.mappedAbsItemId}
			<img
				src="/mappings/cover?abs_item_id={encodeURIComponent(data.mappedAbsItemId)}"
				alt="Audiobook cover"
				class="h-16 w-auto self-start rounded shadow sm:ml-auto"
			/>
		{/if}
	</div>

	<div class="table-wrap">
		<table class="table table-hover" style="table-layout: fixed; width: 100%;">
			<thead>
				<tr>
					<th class="w-10">
						<input
							type="checkbox"
							class="checkbox"
							checked={allSelected}
							indeterminate={someSelected}
							onchange={toggleAll}
							aria-label="Select all on this page"
						/>
					</th>
					{#each columns as col}
						<th
							class="{col.thClass} {col.key ? 'cursor-pointer select-none' : ''} overflow-hidden"
							title={col.label}
							onclick={() => handleSort(col.key)}
						>
							<span class="flex items-center gap-1 min-w-0">
								<span class="truncate">{col.label}</span>
								{#if col.key && sortBy === col.key}
									<span class="shrink-0">{sortDir === 'asc' ? '▲' : '▼'}</span>
								{/if}
							</span>
						</th>
					{/each}
				</tr>
			</thead>
			<tbody>
				{#each items as item (item.id)}
					<tr>
						<td>
							<input
								type="checkbox"
								class="checkbox"
								checked={selectedIds.includes(item.id)}
								onchange={() => toggleRow(item.id)}
								aria-label="Select entry"
							/>
						</td>
						<td class="max-w-xs truncate" title={item.title} use:revealOnTap={item.title}>{item.title}</td>
						<td class="hidden lg:table-cell max-w-xs truncate font-mono text-xs text-surface-500" title={item.document} use:revealOnTap={item.document}>{item.document}</td>
						<td>{formatPercent(item.percentage)}</td>
						<td class="hidden lg:table-cell font-mono text-xs"><div class="progress-scroll-cell"><div class="progress-scroll" title={item.progress} use:scrollIndicator use:revealOnTap={item.progress}>{item.progress}</div></div></td>
						<td class="hidden lg:table-cell truncate" title={item.device} use:revealOnTap={item.device}>{item.device}</td>
						<td class="hidden lg:table-cell">{item.is_latest ? '✓' : ''}</td>
						<td class="truncate" title={formatDate(item.timestamp)} use:revealOnTap={formatDate(item.timestamp)}>{formatDate(item.timestamp)}</td>
						<td
							class="text-center"
							use:revealOnTap={{
								text:
									item.abs_synced === true
										? absSyncedTooltip(item)
										: item.abs_synced === false
											? (item.abs_sync_error ?? 'Sync failed')
											: '',
								always: true,
							}}
						>
							{#if item.abs_synced === true}
								<span title={absSyncedTooltip(item)} class="cursor-default inline-flex items-center justify-center w-5 h-5 rounded-full bg-green-600 text-white text-xs font-bold select-none">!</span>
							{:else if item.abs_synced === false}
								<span title={item.abs_sync_error ?? 'Sync failed'} class="cursor-default inline-flex items-center justify-center w-5 h-5 rounded-full bg-red-600 text-white text-xs font-bold select-none">!</span>
							{/if}
						</td>
						<td>
							<button
								class="btn btn-sm preset-outlined-error-500"
								onclick={() => { pendingDeleteIds = [item.id]; deleteError = null; }}
							>
								Delete
							</button>
						</td>
					</tr>
				{:else}
					<tr>
						<td colspan="10" class="text-center text-surface-500">No progress entries.</td>
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

<Modal
	open={pendingDeleteIds.length > 0}
	onclose={() => { pendingDeleteIds = []; deleteError = null; }}
	labelledby="delete-title"
>
	<h3 class="h3" id="delete-title">
		{pendingDeleteIds.length > 1 ? `Delete ${pendingDeleteIds.length} entries?` : 'Delete entry?'}
	</h3>
	<p class="text-surface-600-400">
		{pendingDeleteIds.length > 1
			? 'This will permanently remove these progress entries. It cannot be undone.'
			: 'This will permanently remove this progress entry. It cannot be undone.'}
	</p>
	{#if deleteError}
		<p class="text-error-500 text-sm">{deleteError}</p>
	{/if}
	<div class="flex justify-end gap-3">
		<button
			type="button"
			class="btn preset-tonal"
			onclick={() => { pendingDeleteIds = []; deleteError = null; }}
		>
			Cancel
		</button>
		<form
			method="POST"
			action="?/deleteRecords"
			use:enhance={() => {
				return async ({ result, update }) => {
					if (result.type === 'success' && result.data?.deleted) {
						pendingDeleteIds = [];
						selectedIds = [];
						deleteError = null;
						await update();
					} else {
						deleteError = 'Failed to delete. Please try again.';
						await update();
					}
				};
			}}
		>
			<input type="hidden" name="ids" value={JSON.stringify(pendingDeleteIds)} />
			<button type="submit" class="btn preset-filled-error-500">Delete</button>
		</form>
	</div>
</Modal>

<style>
	.progress-scroll-cell {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.progress-scroll {
		overflow-x: scroll;
		max-width: 18rem;
		white-space: nowrap;
		scrollbar-width: thin;
		scrollbar-color: rgba(128, 128, 128, 0.6) rgba(128, 128, 128, 0.15);
	}
</style>
