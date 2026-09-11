function BenchmarkInterface() {}

BenchmarkInterface.prototype.Schema = "<a:component type='system'/><empty/>";

// Observation bookkeeping must not participate in simulation serialization or replay hashes.
BenchmarkInterface.prototype.Serialize = null;
BenchmarkInterface.prototype.Init = function()
{
	this.handles = new Map();
	this.handleCounters = new Map();
	this.recording = false;
	this.results = [];
	this.commandTrace = [];
	this.lifecycle = [];
	this.trackedQueues = new Map();
	this.foundations = new Map();
};
BenchmarkInterface.prototype.Deserialize = function()
{
	this.Init();
};

/** Return fresh JSON data. Never call AIInterface, AIProxy, or GUI notification readers. */
BenchmarkInterface.prototype.GetEntity = function(id)
{
	const query = iid => Engine.QueryInterface(id, iid);
	const position = query(IID_Position);
	const point = position?.IsInWorld() ? position.GetPosition2D() : null;
	const health = query(IID_Health);
	const unitAI = query(IID_UnitAI);
	const foundation = query(IID_Foundation);
	const supply = query(IID_ResourceSupply);
	return {
		"id": id,
		"template": Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id),
		"owner": query(IID_Ownership)?.GetOwner() ?? null,
		"classes": query(IID_Identity)?.GetClassesList() ?? [],
		"position": point ? { "x": point.x, "z": point.y } : null,
		"angle": point ? position.GetRotation().y : null,
		"health": health ? { "current": health.GetHitpoints(), "max": health.GetMaxHitpoints() } : null,
		"activity": unitAI?.GetCurrentState() ?? null,
		"idle": unitAI?.IsIdle() ?? null,
		"stance": unitAI?.GetStanceName() ?? null,
		"orders": unitAI?.GetOrders() ?? [],
		"carrying": query(IID_ResourceGatherer)?.GetCarryingStatus() ?? [],
		"queue": query(IID_ProductionQueue)?.GetQueue() ?? [],
		"foundation": foundation ? {
			"progress": foundation.GetBuildPercentage(),
			"builders": foundation.GetBuilders()
		} : null,
		"garrisoned": query(IID_GarrisonHolder)?.GetEntities() ?? [],
		"holder": query(IID_Garrisonable)?.HolderID() ?? null,
		"resource": supply ? {
			"type": supply.GetType(),
			"amount": supply.IsInfinite() ? null : supply.GetCurrentAmount(),
			"infinite": supply.IsInfinite()
		} : null,
		"buildable": query(IID_Builder)?.GetEntitiesList() ?? [],
		"trainable": query(IID_Trainer)?.GetEntitiesList() ?? [],
		"researchable": query(IID_Researcher)?.GetTechnologiesList() ?? []
	};
};

BenchmarkInterface.prototype.GetPlayer = function(seat)
{
	const playerEntity = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetPlayerByID(seat);
	const player = Engine.QueryInterface(playerEntity, IID_Player);
	const identity = Engine.QueryInterface(playerEntity, IID_Identity);
	const technology = Engine.QueryInterface(playerEntity, IID_TechnologyManager);
	const maxPopulation = player.GetMaxPopulation();
	return {
		"seat": seat,
		"name": identity.GetName(),
		"civ": identity.GetCiv(),
		"state": player.GetState(),
		"resources": player.GetResourceCounts(),
		"population": {
			"used": player.GetPopulationCount(),
			"limit": player.GetPopulationLimit(),
			"max": maxPopulation == Infinity ? null : maxPopulation,
			"unlimited": maxPopulation == Infinity
		},
		"researched": technology ? Array.from(technology.GetResearchedTechs()).sort() : [],
		"research_queued": technology ? Array.from(technology.GetQueuedResearch().keys()).sort() : [],
		"statistics": Engine.QueryInterface(playerEntity, IID_StatisticsTracker)?.GetStatistics() ?? null
	};
};

/** Complete current records for gameplay entities, plus an intentionally limited M1 player view. */
BenchmarkInterface.prototype.GetSnapshot = function(seats)
{
	const count = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers();
	const players = Array.from({ "length": count }, (_, seat) => this.GetPlayer(seat));
	const ids = new Set([
		...Engine.GetEntitiesWithInterface(IID_Position),
		...Engine.GetEntitiesWithInterface(IID_Ownership)
	]);
	const entities = Array.from(ids).sort((a, b) => a - b).map(id => this.GetEntity(id));
	const views = {};
	for (const seat of seats)
	{
		if (!Number.isInteger(seat) || seat < 1 || seat >= count)
			throw new Error("Invalid observation seat");
		if (!this.handles.has(seat))
			this.handles.set(seat, new Map());
		const handles = this.handles.get(seat);
		const own = entities.filter(entity => entity.owner == seat);
		const ownIDs = new Set(own.map(entity => entity.id));
		for (const entity of own)
			if (!handles.has(entity.id))
				this.Handle(seat, entity.id);
		views[seat] = {
			"seat": seat,
			"coverage": "current_targets_m2",
			"unavailable_sections": ["last_seen", "map", "events", "orders"],
			"visible_entities": entities.filter(entity => entity.owner != seat &&
				BenchmarkActions.Visible(seat, entity.id)).map(entity => ({
				"handle": this.Handle(seat, entity.id), "template": entity.template,
				"owner": entity.owner, "classes": entity.classes, "position": entity.position,
				"health": entity.health, "resource": entity.resource
			})),
			"action_results": this.results.filter(result => result.seat == seat),
			"action_lifecycle": this.lifecycle.filter(result => result.seat == seat),
			"self": {
				"civ": players[seat].civ,
				"state": players[seat].state,
				"resources": players[seat].resources,
				"population": players[seat].population,
				"researched": players[seat].researched,
				"research_queued": players[seat].research_queued
			},
			"own_entities": own.map(entity => ({
				"handle": handles.get(entity.id),
				"template": entity.template,
				"classes": entity.classes,
				"position": entity.position,
				"health": entity.health,
				"activity": entity.activity,
				"idle": entity.idle,
				"stance": entity.stance,
				"carrying": entity.carrying,
				"buildable": entity.buildable, "trainable": entity.trainable, "researchable": entity.researchable,
				"foundation_progress": entity.foundation?.progress ?? null,
				"holder": ownIDs.has(entity.holder) ? handles.get(entity.holder) : null,
				"garrisoned": entity.garrisoned.filter(id => ownIDs.has(id)).map(id => handles.get(id)),
				"queue": entity.queue.map(item => ({
					"handle": this.QueueHandle(seat, entity.id, item.id),
					"paused": item.paused ?? false, "time_remaining_ms": item.timeRemaining ?? null,
					"needed_population": item.neededSlots ?? null,
					"unit_template": item.unitTemplate ?? null,
					"technology": item.technologyTemplate ?? null,
					"count": item.count ?? null,
					"progress": item.progress ?? null
				}))
			}))
		};
	}
	// Detach component-owned arrays and objects at the boundary.
	return JSON.parse(JSON.stringify({
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"players": views,
		"evaluator": { "players": players, "entities": entities },
		"action_results": this.results, "command_trace": this.commandTrace,
		"action_lifecycle": this.lifecycle
	}));
};

/** Public static rules plus effective values for the bound seat only. */
BenchmarkInterface.prototype.GetCatalog = function(seat, names, technologies)
{
	const templateManager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
	const player = this.GetPlayer(seat);
	const permitted = new Set();
	for (const id of Engine.GetEntitiesWithInterface(IID_Ownership))
		if (Engine.QueryInterface(id, IID_Ownership).GetOwner() == seat)
		{
			permitted.add(templateManager.GetCurrentTemplateName(id));
			for (const iid of [IID_Builder, IID_Trainer])
				for (const name of Engine.QueryInterface(id, iid)?.GetEntitiesList() ?? [])
					permitted.add(name);
		}
	const validName = name => typeof name == "string" && name.length <= 200 &&
		/^[a-z0-9_/-]+$/.test(name) && !name.includes("//");
	const templates = Object.create(null);
	for (const name of names)
	{
		if (!validName(name) && !(name.startsWith("foundation|") && validName(name.slice(11))))
			templates[name] = { "error": "invalid_name" };
		else if (!permitted.has(name) && !name.startsWith("units/" + player.civ + "/") &&
			!name.startsWith("structures/" + player.civ + "/"))
			templates[name] = { "error": "not_permitted" };
		else if (!templateManager.TemplateExists(name))
			templates[name] = { "error": "not_found" };
		else
		{
			const template = templateManager.GetTemplate(name);
			const auras = {};
			for (const aura of template.Auras?._string.split(/\s+/) ?? [])
				if (AuraTemplates.Has(aura))
					auras[aura] = AuraTemplates.Get(aura);
			templates[name] = {
				"static": GetTemplateDataHelper(template, null, auras, Resources),
				"effective": GetTemplateDataHelper(template, seat, auras, Resources)
			};
		}
	}
	const technologyData = Object.create(null);
	for (const name of technologies)
		if (!validName(name))
			technologyData[name] = { "error": "invalid_name" };
		else if (!TechnologyTemplates.Has(name))
			technologyData[name] = { "error": "not_found" };
		else
			technologyData[name] = { "static": GetTechnologyDataHelper(TechnologyTemplates.Get(name), player.civ, Resources) };
	return JSON.parse(JSON.stringify({ "seat": seat, "templates": templates, "technologies": technologyData }));
};

/** Handles are issued only by observations or successful own construction. */
BenchmarkInterface.prototype.Handle = function(seat, id)
{
	if (!this.handles.has(seat))
		this.handles.set(seat, new Map());
	const handles = this.handles.get(seat);
	if (!handles.has(id))
	{
		const next = (this.handleCounters.get(seat) ?? 0) + 1;
		this.handleCounters.set(seat, next);
		handles.set(id, (BenchmarkActions.Owned(seat, id) ? "own-" : "seen-") + next);
	}
	return handles.get(id);
};

BenchmarkInterface.prototype.ResolveHandle = function(seat, handle)
{
	if (typeof handle != "string")
		return undefined;
	return Array.from(this.handles.get(seat) ?? []).find(([, value]) => value == handle)?.[0];
};

BenchmarkInterface.prototype.QueueHandle = function(seat, entity, id)
{
	return this.Handle(seat, entity) + "-q-" + id;
};

BenchmarkInterface.prototype.ResolveQueue = function(seat, entity, handle)
{
	return Engine.QueryInterface(entity, IID_ProductionQueue)?.GetQueue().find(item =>
		this.QueueHandle(seat, entity, item.id) === handle)?.id;
};

BenchmarkInterface.prototype.GetStatus = function(seats)
{
	const count = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers();
	const states = Array.from({ "length": count - 1 }, (_, i) => QueryPlayerIDInterface(i + 1).GetState());
	return {
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"ended": states.includes("won") || states.every(state => state != "active") ||
			seats.every(seat => states[seat - 1] != "active"),
		"player_states": states
	};
};

/** A malformed model batch consumes its interval. Transport timing checks happen in C++. */
BenchmarkInterface.prototype.PrepareDecision = function(json, seats, turn, decision)
{
	this.recording = true;
	this.results = [];
	this.commandTrace = [];
	this.lifecycle = [];
	const batches = json ? JSON.parse(json) : null;
	const commands = [];
	const rejectBatch = (seat, reason) => this.results.push({
		"seat": seat, "decision_id": decision, "action_id": null, "stage": "rejected",
		"reason": reason, "submission_turn": turn, "execution_turn": null
	});
	if (!json)
		return commands;
	if (!Array.isArray(batches) || batches.length != seats.length ||
		new Set(batches.map(batch => batch?.seat)).size != seats.length ||
		batches.some(batch => !seats.includes(batch?.seat)))
	{
		for (const seat of seats)
			rejectBatch(seat, "invalid_batch");
		return commands;
	}
	for (const seat of seats)
	{
		const batch = batches.find(item => item.seat == seat);
		try
		{
			BenchmarkActions.Object(batch, ["seat", "actions"]);
			if (!Array.isArray(batch.actions))
				BenchmarkActions.Reject("invalid_batch");
			if (batch.actions.length > 20)
				BenchmarkActions.Reject("action_budget_exceeded");
			const ids = batch.actions.map(action => action?.action_id);
			if (new Set(ids).size != ids.length)
				BenchmarkActions.Reject("duplicate_action_id");
		}
		catch(error)
		{
			if (!BenchmarkActions.IsRejection(error))
				throw error;
			rejectBatch(seat, error.message);
			continue;
		}
		for (const action of batch.actions)
		{
			const result = {
				"seat": seat, "decision_id": decision,
				"action_id": BenchmarkActions.Identifier(action?.action_id) ? action.action_id : null,
				"submission_turn": turn, "execution_turn": null, "stage": "rejected", "reason": null
			};
			this.results.push(result);
			try
			{
				const command = BenchmarkActions.Translate(this, seat, action);
				result.stage = command ? "submitted" : "applied";
				result.reason = command ? null : "wait";
				if (command)
					commands.push({ "seat": seat, "command": {
						"type": "benchmark-action", "action_id": action.action_id,
						"decision_id": decision, "submission_turn": turn, "command": command
					} });
			}
			catch(error)
			{
				if (!BenchmarkActions.IsRejection(error))
					throw error;
				result.reason = error.message;
			}
		}
	}
	return commands;
};

BenchmarkInterface.prototype.RecordCommand = function(seat, command)
{
	// Playback has no HTTP decision boundaries at which to drain this output buffer.
	if (!this.recording)
		return;
	if (this.commandTrace.length >= 20000)
		throw new Error("Benchmark command trace capacity exceeded");
	this.commandTrace.push({
		"seat": seat, "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"source": this.executing ? "agent" : QueryPlayerIDInterface(seat)?.IsAI() ? "builtin_ai" : "simulation",
		"parent_action_id": this.executing?.action_id ?? null,
		"decision_id": this.executing?.decision_id ?? null,
		"command": clone(command)
	});
};

BenchmarkInterface.prototype.RecordOrder = function(entity, type)
{
	if (this.recording && this.executing)
		this.issuedOrders.set(entity, type);
};

BenchmarkInterface.prototype.ExecuteAction = function(seat, envelope)
{
	if (!this.recording)
		this.results = [];
	let result = this.results.find(item => item.seat == seat && item.decision_id == envelope.decision_id &&
		item.action_id == envelope.action_id);
	if (!result)
	{
		// Normal replay reconstructs results without rerunning observation or HTTP submission.
		result = { "seat": seat, "action_id": envelope.action_id,
			"decision_id": envelope.decision_id, "submission_turn": envelope.submission_turn };
		this.results.push(result);
	}
	result.execution_turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200) + 1;
	result.stage = "failed";
	result.reason = "execution_rejected";
	const command = clone(envelope.command);
	try
	{
		BenchmarkActions.CheckShared(seat, command);
	}
	catch(error)
	{
		if (!BenchmarkActions.IsRejection(error))
			throw error;
		result.reason = error.message;
		return;
	}
	const allEntities = command.entities ?? [];
	command.entities = allEntities.filter(id => BenchmarkActions.CanApply(seat, id, command));
	if (allEntities.length && !command.entities.length)
	{
		result.reason = "unavailable_entity";
		return;
	}
	const producer = command.entity ?? command.entities[0];
	const queue = Engine.QueryInterface(producer ?? INVALID_ENTITY, IID_ProductionQueue);
	const beforeQueue = queue?.GetQueue() ?? [];
	this.executing = envelope;
	this.issuedOrders = new Map();
	let nativeResult;
	try
	{
		nativeResult = ProcessCommand(seat, command);
	}
	finally
	{
		this.executing = null;
	}
	if (!this.recording)
		return;
	const afterQueue = queue?.GetQueue() ?? [];
	const newItem = afterQueue.find(item => !beforeQueue.some(previous => previous.id == item.id));
	const expectedOrder = { "walk": "Walk", "attack-walk": "WalkAndFight", "attack": "Attack",
		"gather": "Gather", "returnresource": "ReturnResource", "repair": "Repair",
		"stop": "Stop", "garrison": "Garrison" }[command.type];
	if (expectedOrder)
	{
		result.entities = allEntities.map(id =>
		{
			const orders = Engine.QueryInterface(id, IID_UnitAI)?.GetOrders() ?? [];
			const applied = command.entities.includes(id) && (this.issuedOrders.get(id) == expectedOrder ||
				orders.some(order => order.type == expectedOrder &&
					(command.target === undefined || order.data?.target == command.target)));
			return { "handle": this.Handle(seat, id), "stage": applied ? "applied" : "failed" };
		});
	}
	else if (command.type == "stance" || command.type == "unload")
		result.entities = allEntities.map(id => ({ "handle": this.Handle(seat, id),
			"stage": command.entities.includes(id) && (command.type == "stance" ?
				Engine.QueryInterface(id, IID_UnitAI)?.GetStanceName() == command.name :
				Engine.QueryInterface(id, IID_Garrisonable)?.HolderID() != command.garrisonHolder) ? "applied" : "failed" }));
	if (result.entities)
	{
		const applied = result.entities.filter(item => item.stage == "applied").length;
		result.stage = applied ? "applied" : "failed";
		result.partial = applied > 0 && applied < result.entities.length;
	}
	else if (newItem && ["train", "research"].includes(command.type))
	{
		result.stage = "applied";
		result.queue_handle = this.QueueHandle(seat, producer, newItem.id);
		this.trackedQueues.set(producer + ":" + newItem.id, { ...result, "producer": producer, "id": newItem.id });
	}
	else if (command.type == "construct" && nativeResult)
	{
		result.stage = "applied";
		result.foundation_handle = this.Handle(seat, nativeResult);
		this.foundations.set(nativeResult, { ...result });
	}
	else if (command.type == "stop-production" && beforeQueue.some(item => item.id == command.id) &&
		!afterQueue.some(item => item.id == command.id))
	{
		result.stage = "applied";
	}
	else if (command.type == "resign" && QueryPlayerIDInterface(seat).GetState() == "defeated")
		result.stage = "applied";
	else if (command.type == "set-rallypoint" &&
		Engine.QueryInterface(command.structures[0], IID_RallyPoint).GetPositions().some(p => p.x == command.x && p.z == command.z))
		result.stage = "applied";
	if (result.stage == "applied")
		result.reason = null;
};

BenchmarkInterface.prototype.OnGlobalConstructionFinished = function(message)
{
	const original = this.foundations.get(message.entity);
	if (!original)
		return;
	// Keep the foundation's player handle when it becomes the completed building.
	const handles = this.handles.get(original.seat);
	handles.set(message.newentity, original.foundation_handle);
	handles.delete(message.entity);
	this.lifecycle.push({ "seat": original.seat, "action_id": original.action_id,
		"decision_id": original.decision_id, "foundation_handle": original.foundation_handle,
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"event": "construction_finished" });
	this.foundations.delete(message.entity);
};

BenchmarkInterface.prototype.RecordQueueLifecycle = function(entity, id, event)
{
	const key = entity + ":" + id;
	const original = this.trackedQueues.get(key);
	if (!original)
		return;
	this.lifecycle.push({ "seat": original.seat, "action_id": original.action_id,
		"decision_id": original.decision_id, "queue_handle": original.queue_handle,
		"event": event, "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() });
	this.trackedQueues.delete(key);
};

BenchmarkInterface.prototype.OnGlobalDestroy = function(message)
{
	for (const record of this.trackedQueues.values())
		if (record.producer == message.entity)
			this.RecordQueueLifecycle(record.producer, record.id, "producer_lost");
	const foundation = this.foundations.get(message.entity);
	if (foundation)
	{
		this.lifecycle.push({ "seat": foundation.seat, "action_id": foundation.action_id,
			"decision_id": foundation.decision_id, "foundation_handle": foundation.foundation_handle,
			"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
			"event": "foundation_lost" });
		this.foundations.delete(message.entity);
	}
};

BenchmarkInterface.prototype.OnGlobalOwnershipChanged = function(message)
{
	if (message.from == message.to)
		return;
	// Stop tracking when the producing entity leaves its original owner's control.
	for (const record of this.trackedQueues.values())
		if (record.producer == message.entity && record.seat == message.from)
			this.RecordQueueLifecycle(record.producer, record.id, "ownership_lost");
	const foundation = this.foundations.get(message.entity);
	if (foundation?.seat == message.from)
	{
		this.lifecycle.push({ "seat": foundation.seat, "action_id": foundation.action_id,
			"decision_id": foundation.decision_id, "foundation_handle": foundation.foundation_handle,
			"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
			"event": "ownership_lost" });
		this.foundations.delete(message.entity);
	}
};

Engine.RegisterSystemComponentType(IID_BenchmarkInterface, "BenchmarkInterface", BenchmarkInterface);
