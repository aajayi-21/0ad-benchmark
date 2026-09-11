/** Pure queries over a detached observation. No engine APIs, handle allocation, or clock changes. */
class BenchmarkObservationQueries
{
	static Inspect(view, query)
	{
		if (query.observation_id !== view.observation_id)
			return { "error": "stale_observation" };
		const common = ["episode_id", "seat", "observation_id", "kind", "cursor", "limit"];
		const fields = { "entities": ["handles"], "region": ["bounds"], "section": ["section"], "briefing": ["max_chars"] };
		if (typeof query.kind != "string" || !Object.hasOwn(fields, query.kind) ||
			Object.keys(query).some(key => ![...common, ...fields[query.kind]].includes(key)))
			return { "error": "invalid_query" };
		const limit = query.limit === undefined ? 32 : query.limit;
		if (!Number.isInteger(limit) || limit < 1 || limit > 64)
			return { "error": "invalid_query" };
		const allEntities = [...view.own_entities, ...view.visible_entities, ...view.last_seen];
		let records;
		switch (query.kind)
		{
		case "entities":
			if (!Array.isArray(query.handles) || query.handles.length > 64 ||
				query.handles.some(handle => typeof handle != "string" || !/^[a-zA-Z0-9_-]{1,80}$/.test(handle)) ||
				new Set(query.handles).size != query.handles.length)
				return { "error": "invalid_query" };
			records = query.handles.map(handle => allEntities.find(entity => entity.handle == handle) ??
				{ "handle": handle, "error": "unavailable_entity" });
			break;
		case "region":
		{
			const bounds = query.bounds;
			const keys = ["min_x", "min_z", "max_x", "max_z"];
			if (!bounds || typeof bounds != "object" || Array.isArray(bounds) ||
				Object.keys(bounds).length != 4 || keys.some(key => !Number.isFinite(bounds[key])) ||
				bounds.min_x < 0 || bounds.min_z < 0 || bounds.max_x > view.map.bounds.max_x || bounds.max_z > view.map.bounds.max_z ||
				bounds.min_x >= bounds.max_x || bounds.min_z >= bounds.max_z)
				return { "error": "invalid_query" };
			const inside = point => point && point.x >= bounds.min_x && point.x < bounds.max_x && point.z >= bounds.min_z && point.z < bounds.max_z;
			records = allEntities.filter(entity => inside(entity.position)).map(entity => ({ "kind": "entity", ...entity }));
			// Cells intersect the half-open bounds; knowledge still refers to their center sample.
			records.push(...view.map.cells.filter(cell => cell.x * view.map.cell_size < bounds.max_x &&
				(cell.x + 1) * view.map.cell_size > bounds.min_x && cell.z * view.map.cell_size < bounds.max_z &&
				(cell.z + 1) * view.map.cell_size > bounds.min_z).map(cell => ({ "kind": "cell", ...cell })));
			break;
		}
		case "section":
			if (!["own_entities", "visible_entities", "last_seen", "events", "action_results", "action_lifecycle", "map_cells", "resource_clusters"].includes(query.section))
				return { "error": "invalid_query" };
			records = query.section == "map_cells" ? view.map.cells : query.section == "resource_clusters" ? view.map.resources : view[query.section];
			break;
		case "briefing": records = this.BriefingRows(view); break;
		default: return { "error": "invalid_query" };
		}
		// The cursor contains only this seat's opaque boundary ID and a permitted-row offset.
		const prefix = view.observation_id + ":";
		let offset = 0;
		if (query.cursor !== undefined && query.cursor !== null)
		{
			if (typeof query.cursor != "string" || !query.cursor.startsWith(prefix) || !/^(0|[1-9][0-9]{0,8})$/.test(query.cursor.slice(prefix.length)))
				return { "error": "invalid_cursor" };
			offset = Number(query.cursor.slice(prefix.length));
		}
		if (offset > records.length)
			return { "error": "invalid_cursor" };
		let selected = records.slice(offset, offset + limit);
		if (query.kind == "briefing")
		{
			const budget = query.max_chars === undefined ? 12000 : query.max_chars;
			if (!Number.isInteger(budget) || budget < 1024 || budget > 32768)
				return { "error": "invalid_query" };
			let length = 0;
			selected = selected.filter((row, index) =>
			{
				length += row.length + (index ? 1 : 0);
				return length <= budget;
			});
			// Do not skip or silently clip an oversized row. The caller can raise its budget.
			if (!selected.length && offset < records.length)
				return { "error": "text_budget_too_small" };
		}
		const next = offset + selected.length;
		return JSON.parse(JSON.stringify({
			"observation_id": view.observation_id, "offset": offset, "total_count": records.length,
			"omitted_count": records.length - selected.length,
			"remaining_count": records.length - next,
			"next_cursor": next < records.length ? prefix + next : null,
			...(query.kind == "briefing" ? { "text": selected.join("\n"),
				"details": "Compact entity rows omit orders, queues, carrying, capabilities and garrison details; inspect_entities returns the full permitted record. Map cells use inspect_region or section=map_cells." } : { "records": selected })
		}));
	}

	static BriefingRows(view)
	{
		const rows = [
			"Observation " + view.observation_id + "; seat " + view.seat + "; turn " + view.turn +
				"; simulation " + view.sim_time_ms + " ms; track " + view.track,
			"Objective: " + JSON.stringify(view.objective),
			"Resources: " + JSON.stringify(view.self.resources),
			"Population: " + JSON.stringify(view.self.population),
			"Phase: " + view.self.phase + "; action budget " + view.action_budget.remaining + "; read budget belongs to runner."
		];
		const groups = new Map();
		for (const entity of view.own_entities)
		{
			const key = entity.template;
			if (!groups.has(key))
				groups.set(key, { "template": key, "count": 0, "idle": 0, "queued_items": 0 });
			const group = groups.get(key);
			++group.count;
			group.idle += entity.idle ? 1 : 0;
			group.queued_items += entity.queue.length;
		}
		rows.push("Idle workers: " + view.own_entities.filter(entity => entity.idle && entity.buildable.length).length);
		for (const result of view.action_results)
			rows.push("Action: " + JSON.stringify(result));
		for (const event of view.action_lifecycle)
			rows.push("Action event: " + JSON.stringify(event));
		const compact = (entity, observedTurn = view.turn) => ({ "handle": entity.handle, "template": entity.template,
			"owner": entity.owner ?? view.seat, "position": entity.position, "health": entity.health,
			"status": entity.status ?? ((entity.owner ?? view.seat) == view.seat ? "owned" : "visible"),
			"last_seen_turn": entity.last_seen_turn ?? observedTurn });
		for (const event of view.events)
			rows.push("Event: " + JSON.stringify({ "event_id": event.event_id, "type": event.type,
				"turn": event.turn, "entity": compact(event.entity, event.observed_turn) }));
		rows.push("Omitted interval events: " + view.omitted_events);
		for (const entity of view.visible_entities.filter(item => view.self.diplomacy[item.owner] < 0 && item.owner != 0))
			rows.push("Visible threat: " + JSON.stringify(compact(entity)));
		for (const group of Array.from(groups.values()).sort((a, b) => a.template.localeCompare(b.template)))
			rows.push("Own group: " + JSON.stringify(group));
		for (const entity of view.own_entities)
			rows.push("Own entity: " + JSON.stringify({ ...compact(entity), "activity": entity.activity,
				"idle": entity.idle, "queued_items": entity.queue.length, "foundation_progress": entity.foundation_progress }));
		for (const entity of view.visible_entities)
			rows.push("Visible entity: " + JSON.stringify(compact(entity)));
		for (const entity of view.last_seen)
			rows.push("Memory: " + JSON.stringify(compact(entity)));
		rows.push("Map: " + JSON.stringify({ "bounds": view.map.bounds, "cell_size": view.map.cell_size,
			"coordinates": view.map.coordinates, "visible_cells": view.map.cells.filter(cell => cell.visibility == "visible").length,
			"fogged_cells": view.map.cells.filter(cell => cell.visibility == "fogged").length,
			"unknown_cells": view.map.cells.filter(cell => cell.visibility == "unknown").length }));
		for (const cluster of view.map.resources)
			rows.push("Known resources: " + JSON.stringify(cluster));
		rows.push("Unavailable fields: " + view.unavailable_fields.join(", "));
		return rows;
	}
}

Engine.RegisterGlobal("BenchmarkObservationQueries", BenchmarkObservationQueries);
