/** Independent, non-serialized player knowledge. No AI/GUI buffers or fogged live data. */
class BenchmarkObservations
{
	constructor(component)
	{
		this.component = component;
		this.seats = new Map();
		this.views = {};
		this.catalogs = new Map();
		this.turn = 0;
	}

	static Copy(value)
	{
		return JSON.parse(JSON.stringify(value));
	}

	static RealEntity(id)
	{
		return !Engine.QueryInterface(id, IID_Mirage) && !!Engine.QueryInterface(id, IID_Identity) &&
			!!Engine.QueryInterface(id, IID_Position);
	}

	static PublicEntity(id, handle)
	{
		const position = Engine.QueryInterface(id, IID_Position);
		const point = position?.IsInWorld() ? position.GetPosition2D() : null;
		const health = Engine.QueryInterface(id, IID_Health);
		const resource = Engine.QueryInterface(id, IID_ResourceSupply);
		return {
			"handle": handle,
			"template": Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id),
			"owner": Engine.QueryInterface(id, IID_Ownership)?.GetOwner() ?? null,
			"classes": Engine.QueryInterface(id, IID_Identity)?.GetClassesList() ?? [],
			"position": point ? { "x": point.x, "z": point.y } : null,
			"health": health ? { "current": health.GetHitpoints(), "max": health.GetMaxHitpoints() } : null,
			"resource": resource ? { "type": resource.GetType(),
				"amount": resource.IsInfinite() ? null : resource.GetCurrentAmount(),
				"infinite": resource.IsInfinite() } : null
		};
	}

	BeginDecision()
	{
		for (const knowledge of this.seats.values())
		{
			knowledge.events = [];
			knowledge.omittedEvents = 0;
		}
	}

	Event(knowledge, type, facts, turn = this.turn, observedTurn = turn)
	{
		// Count only permitted events. Inspection never drains or appends this buffer.
		const id = ++knowledge.eventCounter;
		if (knowledge.events.length == 4096)
		{
			++knowledge.omittedEvents;
			return;
		}
		knowledge.events.push(BenchmarkObservations.Copy({
			"event_id": "event-" + id, "type": type, "turn": turn,
			"sim_time_ms": turn * 200, "observed_turn": observedTurn, "entity": facts
		}));
	}

	CanSee(seat, id)
	{
		return BenchmarkObservations.RealEntity(id) &&
			(InitAttributes.benchmark.information_mode == "full" || BenchmarkActions.Visible(seat, id));
	}

	PositionVisibility(seat, point)
	{
		if (InitAttributes.benchmark.information_mode == "full")
			return "visible";
		return Engine.QueryInterface(SYSTEM_ENTITY, IID_RangeManager).GetLosVisibilityPosition(point.x, point.z, seat);
	}

	Capture(seats, turn)
	{
		this.turn = turn;
		const ids = Engine.GetEntitiesWithInterface(IID_Identity).filter(BenchmarkObservations.RealEntity).sort((a, b) => a - b);
		for (const seat of seats)
		{
			if (!this.seats.has(seat))
				this.seats.set(seat, { "memory": new Map(), "cells": [], "events": [],
					"omittedEvents": 0, "eventCounter": 0 });
			const knowledge = this.seats.get(seat);
			const own = ids.filter(id => BenchmarkActions.Owned(seat, id));
			const visible = ids.filter(id => !BenchmarkActions.Owned(seat, id) && this.CanSee(seat, id));
			const current = new Set([...own, ...visible]);
			for (const id of [...own, ...visible])
			{
				const owned = BenchmarkActions.Owned(seat, id);
				const handle = this.component.Handle(seat, id);
				const facts = BenchmarkObservations.Copy(BenchmarkObservations.PublicEntity(id, handle));
				const previous = knowledge.memory.get(id);
				if ((!previous || !["visible", "owned"].includes(previous.status)) && !owned)
					this.Event(knowledge, "sighting", facts);
				knowledge.memory.set(id, { "facts": facts, "status": owned ? "owned" : "visible",
					"last_seen_turn": turn, "last_seen_time_ms": turn * 200,
					"positionWasVisible": facts.position && this.PositionVisibility(seat, facts.position) == "visible" });
			}
			for (const [id, record] of knowledge.memory)
			{
				if (current.has(id) || record.status == "destroyed")
					continue;
				if (["visible", "owned"].includes(record.status))
				{
					record.status = "last_seen";
					this.Event(knowledge, "lost_sight", record.facts, this.turn, record.last_seen_turn);
				}
				// Absence at a revisited position is evidence about that position, not death.
				const visiblePosition = record.facts.position && this.PositionVisibility(seat, record.facts.position) == "visible";
				if (visiblePosition && !record.positionWasVisible && record.status == "last_seen")
				{
					record.status = "not_present_at_last_position";
					this.Event(knowledge, "not_present_at_last_position", record.facts, this.turn, record.last_seen_turn);
				}
				record.positionWasVisible = visiblePosition;
			}
			this.CaptureMap(seat, knowledge);
		}
	}

	CaptureMap(seat, knowledge)
	{
		const terrain = Engine.QueryInterface(SYSTEM_ENTITY, IID_Terrain);
		const water = Engine.QueryInterface(SYSTEM_ENTITY, IID_WaterManager);
		const territory = Engine.QueryInterface(SYSTEM_ENTITY, IID_TerritoryManager);
		const size = terrain.GetMapSize();
		const cellSize = InitAttributes.benchmark.map_cell_size;
		const side = Math.ceil(size / cellSize);
		for (let z = 0; z < side; ++z)
			for (let x = 0; x < side; ++x)
			{
				const index = z * side + x;
				const point = { "x": Math.min((x + 0.5) * cellSize, size - 0.01),
					"z": Math.min((z + 0.5) * cellSize, size - 0.01) };
				const visibility = this.PositionVisibility(seat, point);
				if (!knowledge.cells[index])
					knowledge.cells[index] = { "x": x, "z": z, "visibility": "unknown",
						"terrain": null, "elevation_m": null, "water_depth_m": null, "territory": null };
				const cell = knowledge.cells[index];
				cell.visibility = visibility == "hidden" ? "unknown" : visibility;
				if (visibility != "visible")
					continue;
				const elevation = terrain.GetGroundLevel(point.x, point.z);
				const depth = Math.max(0, water.GetWaterLevel(point.x, point.z) - elevation);
				cell.terrain = depth > 0 ? "water" : "land";
				cell.elevation_m = elevation;
				cell.water_depth_m = depth;
				cell.territory = { "owner": territory.GetOwner(point.x, point.z), "last_seen_turn": this.turn };
			}
	}

	OwnEntity(seat, entity, ownIDs)
	{
		const handles = this.component.handles.get(seat);
		const knownTarget = id =>
		{
			const record = this.seats.get(seat).memory.get(id);
			return record && ["owned", "visible"].includes(record.status) ? record.facts.handle : null;
		};
		return {
			"handle": handles.get(entity.id), "template": entity.template, "classes": entity.classes,
			"position": entity.position, "angle": entity.angle, "health": entity.health,
			"activity": entity.activity, "idle": entity.idle, "stance": entity.stance,
			"orders": entity.orders.map(order => ({
				"type": order.type,
				// UnitAI's lastPos, routes, search results, etc. are deliberately unavailable.
				"target": knownTarget(order.data?.target),
				"position": ["Walk", "WalkAndFight", "Patrol"].includes(order.type) &&
					Number.isFinite(order.data?.x) && Number.isFinite(order.data?.z) ?
					{ "x": order.data.x, "z": order.data.z } : null
			})),
			"carrying": entity.carrying,
			"buildable": entity.buildable, "trainable": entity.trainable, "researchable": entity.researchable,
			"foundation_progress": entity.foundation?.progress ?? null,
			"holder": ownIDs.has(entity.holder) ? handles.get(entity.holder) : null,
			"garrisoned": entity.garrisoned.filter(id => ownIDs.has(id)).map(id => handles.get(id)),
			"queue": entity.queue.map(item => ({
				"handle": this.component.QueueHandle(seat, entity.id, item.id),
				"paused": item.paused ?? false, "time_remaining_ms": item.timeRemaining ?? null,
				"needed_population": item.neededSlots ?? null, "unit_template": item.unitTemplate ?? null,
				"technology": item.technologyTemplate ?? null, "count": item.count ?? null, "progress": item.progress ?? null
			}))
		};
	}

	Freeze(seats, episode)
	{
		const config = InitAttributes.benchmark;
		const ended = this.component.GetStatus(seats).ended || this.turn >= config.turn_limit;
		for (const seat of seats)
		{
			const knowledge = this.seats.get(seat);
			const ownIDs = new Set(Engine.GetEntitiesWithInterface(IID_Identity).filter(id => BenchmarkActions.Owned(seat, id)));
			const own = Array.from(ownIDs).sort((a, b) => a - b).map(id => this.component.GetEntity(id));
			const player = this.component.GetPlayer(seat);
			const diplomacy = QueryPlayerIDInterface(seat, IID_Diplomacy);
			const limits = QueryPlayerIDInterface(seat, IID_EntityLimits);
			const population = own.reduce((sum, entity) => sum + (Engine.QueryInterface(entity.id, IID_Cost)?.GetPopCost() ?? 0), 0);
			this.FreezeCatalog(seat, own, player.civ);
			this.views[seat] = BenchmarkObservations.Copy({
				"schema_version": "1.0", "observation_id": episode + "-p" + seat + "-t" + this.turn,
				"episode_id": episode, "seat": seat, "turn": this.turn, "sim_time_ms": this.turn * 200,
				"information_mode": config.information_mode,
				"track": config.information_mode == "full" ? "full_diagnostic" :
					QueryPlayerIDInterface(seat).IsAI() ? "native_ai_diagnostic" : "partial",
				"coverage": "turn_memory_m3",
				"unavailable_fields": ["enemy_private_state", "nonvisible_order_targets", "unitai_internal_order_data", "native_pathfinder_grid",
					"terrain_texture", "unseen_explored_cell_terrain", "runner_tool_budget"],
				"objective": { "description": config.objective,
					"victory_conditions": InitAttributes.settings.VictoryConditions ?? [], "turn_limit": config.turn_limit },
				"action_budget": { "remaining": player.state == "active" && !ended ? 20 : 0 },
				"tool_budget": { "remaining": null, "status": "runner_not_attached" },
				"self": {
					"civ": player.civ, "state": player.state, "resources": player.resources,
					"population": { ...player.population, "actual_units": own.filter(entity => entity.classes.includes("Unit")).length,
						"entity_population": population, "reserved_population": player.population.used - population },
					"phase": player.researched.some(name => name.startsWith("phase_city")) ? "city" :
						player.researched.some(name => name.startsWith("phase_town")) ? "town" : "village",
					"researched": player.researched, "research_queued": player.research_queued,
					"diplomacy": diplomacy.GetDiplomacy(), "team": diplomacy.GetTeam(), "team_locked": diplomacy.IsTeamLocked(),
					"entity_limits": { "limits": limits?.GetLimits() ?? {}, "counts": limits?.GetCounts() ?? {} }
				},
				"other_players": Array.from({ "length": Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers() - 1 }, (_, i) => i + 1)
					.filter(other => other != seat).map(other => ({
						"seat": other, "civ": QueryPlayerIDInterface(other, IID_Identity).GetCiv(),
						"team": QueryPlayerIDInterface(other, IID_Diplomacy).GetTeam(),
						"diplomacy": diplomacy.GetDiplomacy()[other], "state": QueryPlayerIDInterface(other).GetState()
					})),
				"own_entities": own.map(entity => this.OwnEntity(seat, entity, ownIDs)),
				"visible_entities": Array.from(knowledge.memory.values()).filter(record => record.status == "visible").map(record => record.facts),
				"last_seen": Array.from(knowledge.memory.values()).filter(record => !["visible", "owned"].includes(record.status)).map(record => ({
					...record.facts, "status": record.status, "last_seen_turn": record.last_seen_turn, "last_seen_time_ms": record.last_seen_time_ms
				})),
				"events": knowledge.events, "omitted_events": knowledge.omittedEvents,
				"action_results": this.component.results.filter(result => result.seat == seat),
				"action_lifecycle": this.component.lifecycle.filter(result => result.seat == seat),
				"map": { "bounds": { "min_x": 0, "min_z": 0,
					"max_x": Engine.QueryInterface(SYSTEM_ENTITY, IID_Terrain).GetMapSize(),
					"max_z": Engine.QueryInterface(SYSTEM_ENTITY, IID_Terrain).GetMapSize() },
				"coordinates": "world meters (x,z); origin (0,0); positive x and z follow engine axes; angles in radians",
				"cell_size": config.map_cell_size, "sampling": "cell_center", "cells": knowledge.cells,
				"resources": this.ResourceClusters(knowledge) },
				"catalog": { "templates": Object.keys(this.catalogs.get(seat).templates).sort(),
					"technologies": Object.keys(this.catalogs.get(seat).technologies).sort() }
			});
		}
	}

	ResourceClusters(knowledge)
	{
		const clusters = new Map();
		for (const record of knowledge.memory.values())
		{
			const { resource, position } = record.facts;
			if (!resource || !position || ["destroyed", "not_present_at_last_position"].includes(record.status))
				continue;
			const x = Math.floor(position.x / InitAttributes.benchmark.map_cell_size);
			const z = Math.floor(position.z / InitAttributes.benchmark.map_cell_size);
			const key = x + ":" + z + ":" + resource.type.generic + ":" + resource.type.specific;
			if (!clusters.has(key))
				clusters.set(key, { "x": x, "z": z, "type": resource.type, "entities": 0,
					"known_finite_amount": 0, "infinite_entities": 0, "handles": [] });
			const cluster = clusters.get(key);
			++cluster.entities;
			cluster.known_finite_amount += resource.amount ?? 0;
			cluster.infinite_entities += resource.infinite ? 1 : 0;
			cluster.handles.push(record.facts.handle);
		}
		return Array.from(clusters.values());
	}

	FreezeCatalog(seat, own, civ)
	{
		const manager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
		if (!this.templateNames)
			this.templateNames = manager.FindAllTemplates(false);
		const names = new Set(this.templateNames.filter(name => name.startsWith("units/" + civ + "/") || name.startsWith("structures/" + civ + "/")));
		for (const entity of own)
			for (const name of [entity.template, ...entity.buildable, ...entity.trainable])
				names.add(name);
		this.catalogs.set(seat, this.component.BuildCatalog(seat, Array.from(names).sort(), Object.keys(TechnologyTemplates.GetAll()).sort()));
	}

	Catalog(seat, names, technologies)
	{
		const cache = this.catalogs.get(seat);
		const civ = this.views[seat].self.civ;
		const valid = name => typeof name == "string" && name.length <= 200 && /^[a-z0-9_/-]+$/.test(name) && !name.includes("//");
		return BenchmarkObservations.Copy({ "seat": seat,
			"templates": Object.fromEntries(names.map(name => [name, (Object.hasOwn(cache.templates, name) ? cache.templates[name] : null) ?? { "error":
				!valid(name) && !(name.startsWith("foundation|") && valid(name.slice(11))) ? "invalid_name" :
					name.startsWith("units/" + civ + "/") || name.startsWith("structures/" + civ + "/") ? "not_found" : "not_permitted" }])),
			"technologies": Object.fromEntries(technologies.map(name => [name, (Object.hasOwn(cache.technologies, name) ? cache.technologies[name] : null) ??
				{ "error": valid(name) ? "not_found" : "invalid_name" }]))
		});
	}

	Rename(message)
	{
		if (!BenchmarkObservations.RealEntity(message.entity) || !BenchmarkObservations.RealEntity(message.newentity))
			return;
		for (const [seat, knowledge] of this.seats)
		{
			const handles = this.component.handles.get(seat);
			const handle = handles?.get(message.entity);
			if (!handle || !(BenchmarkActions.Owned(seat, message.entity) ||
				this.CanSee(seat, message.entity) && this.CanSee(seat, message.newentity)))
				continue;
			handles.set(message.newentity, handle);
			handles.delete(message.entity);
			const record = knowledge.memory.get(message.entity);
			knowledge.memory.delete(message.entity);
			if (record)
				knowledge.memory.set(message.newentity, record);
			this.Event(knowledge, "renamed", BenchmarkObservations.PublicEntity(message.newentity, handle), this.turn + 1);
		}
	}

	Destroy(id, formerOwner)
	{
		if (!BenchmarkObservations.RealEntity(id))
			return;
		for (const [seat, knowledge] of this.seats)
		{
			if (formerOwner != seat && !BenchmarkActions.Owned(seat, id) && !this.CanSee(seat, id))
				continue;
			const record = knowledge.memory.get(id);
			if (!record || record.status == "destroyed")
				continue;
			record.status = "destroyed";
			this.Event(knowledge, "destroyed", record.facts, this.turn + 1, record.last_seen_turn);
		}
	}

	OwnershipChanged(message)
	{
		if (!BenchmarkObservations.RealEntity(message.entity) || message.from == message.to)
			return;
		if (message.to == -1)
		{
			this.Destroy(message.entity, message.from);
			return;
		}
		const knowledge = this.seats.get(message.from);
		const record = knowledge?.memory.get(message.entity);
		if (record)
		{
			record.status = "last_seen";
			this.Event(knowledge, "ownership_lost", record.facts, this.turn + 1, record.last_seen_turn);
		}
	}
}

Engine.RegisterGlobal("BenchmarkObservations", BenchmarkObservations);
