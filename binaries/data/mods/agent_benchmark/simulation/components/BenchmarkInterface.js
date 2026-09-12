function BenchmarkInterface() {}

BenchmarkInterface.prototype.Schema = "<a:component type='system'/><empty/>";

// Observation bookkeeping must not participate in simulation serialization or replay hashes.
BenchmarkInterface.prototype.Serialize = null;
BenchmarkInterface.prototype.Init = function()
{
	this.handles = new Map();
	this.handleCounters = new Map();
	this.queueHandles = new Map();
	this.observations = new BenchmarkObservations(this);
	this.recording = false;
	this.results = [];
	this.commandTrace = [];
	this.lifecycle = [];
	this.trackedQueues = new Map();
	this.foundations = new Map();
	// Privileged event ledger: sequence numbers are episode-monotonic; the buffer holds one interval.
	this.ledger = [];
	this.ledgerSequence = 0;
	this.ledgerOverflow = 0;
	this.lastAttacks = new Map();
	this.renamedAway = new Set();
	this.intervalMetrics = new Map();
	// The template manager forgets an entity before this component sees MT_Destroy, so names
	// are cached at creation. Static template costs give a fixed valuation for losses.
	this.templateNames = new Map();
	this.staticCosts = new Map();
	// Native Ownership releases the owner while MT_Destroy is dispatched, before this component
	// runs; the release message records the former owner for the destroy record.
	this.formerOwners = new Map();
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

/** Capture knowledge only after completed engine turns, never during inspection. */
BenchmarkInterface.prototype.UpdateKnowledge = function(seats, turn)
{
	this.observations.Capture(seats, turn);
	this.AccumulateMetrics();
	return true;
};

BenchmarkInterface.prototype.FreezeObservation = function(seats, episode)
{
	this.observations.Freeze(seats, episode);
	return true;
};

BenchmarkInterface.prototype.Inspect = function(seat, query)
{
	return BenchmarkObservationQueries.Inspect(this.observations.views[seat], JSON.parse(query));
};

/** Evaluator records are private; player records come only from the frozen cache. */
BenchmarkInterface.prototype.GetSnapshot = function(seats)
{
	const count = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers();
	const players = Array.from({ "length": count }, (_, seat) => this.GetPlayer(seat));
	const ids = new Set([
		...Engine.GetEntitiesWithInterface(IID_Position),
		...Engine.GetEntitiesWithInterface(IID_Ownership)
	]);
	const entities = Array.from(ids).sort((a, b) => a - b).map(id => this.GetEntity(id));
	return JSON.parse(JSON.stringify({
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"players": Object.fromEntries(seats.map(seat => [seat, this.observations.views[seat]])),
		"evaluator": { "players": players, "entities": entities, "player_states": this.GetStatus(seats).player_states },
		"action_results": this.results, "command_trace": this.commandTrace,
		"action_lifecycle": this.lifecycle,
		"telemetry": this.TelemetryConfigured(),
		"ledger": {
			"first_seq": this.ledger.length ? this.ledger[0].seq : this.ledgerSequence + 1,
			"last_seq": this.ledgerSequence, "overflow": this.ledgerOverflow, "events": this.ledger
		},
		"interval_metrics": Object.fromEntries(this.intervalMetrics)
	}));
};

BenchmarkInterface.prototype.GetCatalog = function(seat, names, technologies)
{
	return this.observations.Catalog(seat, names, technologies);
};

/** Public static rules plus effective values for the bound seat only. */
BenchmarkInterface.prototype.BuildCatalog = function(seat, names, technologies)
{
	const templateManager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
	const player = this.GetPlayer(seat);
	const permitted = new Set();
	for (const id of Engine.GetEntitiesWithInterface(IID_Ownership))
		if (BenchmarkActions.Owned(seat, id))
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
	if (!this.queueHandles.has(seat))
		this.queueHandles.set(seat, new Map());
	const handles = this.queueHandles.get(seat);
	const key = entity + ":" + id;
	if (!handles.has(key))
		handles.set(key, "queue-" + (handles.size + 1));
	return handles.get(key);
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
	this.observations.BeginDecision();
	this.results = [];
	this.commandTrace = [];
	this.lifecycle = [];
	this.ledger = [];
	this.ledgerOverflow = 0;
	this.intervalMetrics = new Map();
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
		"seat": seat, "turn": this.InProgressTurn(),
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
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
	this.Record("construction_finished", { "entity": this.Facts(message.newentity), "foundation_id": message.entity });
	const original = this.foundations.get(message.entity);
	if (!original)
		return;
	// Keep the foundation's player handle when it becomes the completed building.
	const handles = this.handles.get(original.seat);
	handles.set(message.newentity, original.foundation_handle);
	handles.delete(message.entity);
	this.lifecycle.push({ "seat": original.seat, "action_id": original.action_id,
		"decision_id": original.decision_id, "foundation_handle": original.foundation_handle,
		"turn": this.InProgressTurn(), "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
		"event": "construction_finished" });
	this.foundations.delete(message.entity);
};

BenchmarkInterface.prototype.RecordQueueLifecycle = function(entity, id, event)
{
	// The public ProductionQueue hook reports every producer; tracked-queue events are agent feedback.
	if (event == "cancelled" || event == "production_finished")
		this.Record("queue_" + event, { "entity": this.Facts(entity), "item_id": id });
	const key = entity + ":" + id;
	const original = this.trackedQueues.get(key);
	if (!original)
		return;
	this.lifecycle.push({ "seat": original.seat, "action_id": original.action_id,
		"decision_id": original.decision_id, "queue_handle": original.queue_handle, "event": event,
		"turn": this.InProgressTurn(), "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() });
	this.trackedQueues.delete(key);
};

BenchmarkInterface.prototype.OnGlobalDestroy = function(message)
{
	this.observations.Destroy(message.entity);
	this.RecordDestroy(message.entity);
	for (const record of this.trackedQueues.values())
		if (record.producer == message.entity)
			this.RecordQueueLifecycle(record.producer, record.id, "producer_lost");
	const foundation = this.foundations.get(message.entity);
	if (foundation)
	{
		this.lifecycle.push({ "seat": foundation.seat, "action_id": foundation.action_id,
			"decision_id": foundation.decision_id, "foundation_handle": foundation.foundation_handle,
			"turn": this.InProgressTurn(), "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
			"event": "foundation_lost" });
		this.foundations.delete(message.entity);
	}
};

BenchmarkInterface.prototype.OnGlobalOwnershipChanged = function(message)
{
	this.observations.OwnershipChanged(message);
	if (message.from == message.to)
		return;
	this.RecordOwnership(message);
	// Stop tracking when the producing entity leaves its original owner's control.
	for (const record of this.trackedQueues.values())
		if (record.producer == message.entity && record.seat == message.from)
			this.RecordQueueLifecycle(record.producer, record.id, "ownership_lost");
	const foundation = this.foundations.get(message.entity);
	if (foundation?.seat == message.from)
	{
		this.lifecycle.push({ "seat": foundation.seat, "action_id": foundation.action_id,
			"decision_id": foundation.decision_id, "foundation_handle": foundation.foundation_handle,
			"turn": this.InProgressTurn(), "sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
			"event": "ownership_lost" });
		this.foundations.delete(message.entity);
	}
};

BenchmarkInterface.prototype.OnGlobalEntityRenamed = function(message)
{
	this.observations.Rename(message);
	if (!this.LedgerEntity(message.entity))
		return;
	this.renamedAway.add(message.entity);
	this.Record("renamed", {
		"entity": this.Facts(message.entity), "new_entity": this.Facts(message.newentity),
		"kind": Engine.QueryInterface(message.entity, IID_Foundation) ? "construction_finished" :
			Engine.QueryInterface(message.entity, IID_Promotion) ? "promotion" : "template_change"
	});
};

/**
 * Privileged telemetry ledger. Handlers only read simulation state; with telemetry disabled
 * they return immediately, so the gameplay projection is identical either way. Replay playback
 * never drains the buffer, so nothing is recorded outside a live decision interval.
 */
BenchmarkInterface.prototype.TelemetryConfigured = function()
{
	return InitAttributes.benchmark?.telemetry !== false;
};

BenchmarkInterface.prototype.TelemetryEnabled = function()
{
	return this.recording && this.TelemetryConfigured();
};

/**
 * The turn currently being simulated. Timer time already includes this turn during the update
 * phase but not during the command flush, so the completed-turn counter is the stable reference.
 */
BenchmarkInterface.prototype.InProgressTurn = function()
{
	return this.observations.turn + 1;
};

BenchmarkInterface.prototype.LedgerEntity = function(id)
{
	return this.TelemetryEnabled() && !Engine.QueryInterface(id, IID_Mirage) && !!Engine.QueryInterface(id, IID_Identity);
};

BenchmarkInterface.prototype.OnGlobalCreate = function(message)
{
	const name = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(message.entity);
	if (name)
		this.templateNames.set(message.entity, name);
};

BenchmarkInterface.prototype.StaticCost = function(template)
{
	if (!template)
		return null;
	if (!this.staticCosts.has(template))
	{
		const resources = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetTemplate(template)?.Cost?.Resources;
		this.staticCosts.set(template, resources ? Object.fromEntries(Object.keys(resources)
			.filter(key => !key.startsWith("@")).map(key => [key, +resources[key]])) : null);
	}
	return this.staticCosts.get(template);
};

BenchmarkInterface.prototype.Facts = function(id)
{
	if (!Engine.QueryInterface(id, IID_Identity))
		return { "id": id };
	const position = Engine.QueryInterface(id, IID_Position);
	const point = position?.IsInWorld() ? position.GetPosition2D() : null;
	const health = Engine.QueryInterface(id, IID_Health);
	const template = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id) ||
		this.templateNames.get(id) || null;
	return {
		"id": id,
		"template": template,
		"owner": Engine.QueryInterface(id, IID_Ownership)?.GetOwner() ?? null,
		"classes": Engine.QueryInterface(id, IID_Identity).GetClassesList(),
		"position": point ? { "x": point.x, "z": point.y } : null,
		"health": health ? { "current": health.GetHitpoints(), "max": health.GetMaxHitpoints() } : null,
		"cost": this.StaticCost(template),
		"visible_to": Array.from(this.observations.seats.keys()).filter(seat =>
			BenchmarkActions.Owned(seat, id) || this.observations.CanSee(seat, id))
	};
};

BenchmarkInterface.prototype.Record = function(type, fields)
{
	if (!this.TelemetryEnabled())
		return;
	const sequence = ++this.ledgerSequence;
	// Overflow is reported, never silently dropped; the runner invalidates affected scored runs.
	if (this.ledger.length >= 100000)
	{
		++this.ledgerOverflow;
		return;
	}
	this.ledger.push(JSON.parse(JSON.stringify({
		"seq": sequence, "turn": this.InProgressTurn(),
		"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(), "type": type, ...fields
	})));
};

BenchmarkInterface.prototype.RecordDestroy = function(id)
{
	const name = this.templateNames.get(id);
	this.templateNames.delete(id);
	const owner = this.formerOwners.get(id);
	this.formerOwners.delete(id);
	if (!this.LedgerEntity(id))
		return;
	const attack = this.lastAttacks.get(id) ?? null;
	this.lastAttacks.delete(id);
	const renamed = this.renamedAway.delete(id);
	const health = Engine.QueryInterface(id, IID_Health);
	// A rename replaces an entity; it is not a casualty. A zero-health death without a recorded
	// attacker was caused by a script, decay, or another non-combat mechanic.
	const cause = renamed ? "renamed" : health && !health.GetHitpoints() ?
		(attack && attack.turn == this.InProgressTurn() ? "killed" : "died") : "removed";
	const facts = this.Facts(id);
	if (owner !== undefined)
	{
		facts.owner = owner;
		if (this.observations.seats.has(owner) && !facts.visible_to.includes(owner))
			facts.visible_to.push(owner);
	}
	this.Record("destroyed", { "entity": { ...facts, "template": name ?? facts.template }, "cause": cause, "killer": attack });
};

BenchmarkInterface.prototype.RecordOwnership = function(message)
{
	if (message.to == INVALID_PLAYER)
	{
		this.formerOwners.set(message.entity, message.from);
		return;
	}
	if (!this.LedgerEntity(message.entity))
		return;
	const attack = this.lastAttacks.get(message.entity);
	const kind = message.from == INVALID_PLAYER ? "created" :
		attack?.capture && attack.turn >= this.InProgressTurn() - 1 ? "captured" :
			message.to == 0 && message.from > 0 ? "transferred_to_gaia" : "owner_changed";
	this.Record("ownership_changed", { "entity": this.Facts(message.entity),
		"from": message.from, "to": message.to, "kind": kind });
};

BenchmarkInterface.prototype.OnGlobalAttacked = function(message)
{
	if (!this.LedgerEntity(message.target))
		return;
	const attack = { "attacker": message.attacker, "attacker_owner": message.attackerOwner,
		"turn": this.InProgressTurn(), "capture": message.capture > 0 };
	this.lastAttacks.set(message.target, attack);
	this.Record("attacked", {
		"entity": this.Facts(message.target), "attacker": this.Facts(message.attacker),
		"attacker_owner": message.attackerOwner, "attack_type": message.type,
		"damage": message.damage, "capture": message.capture, "from_status_effect": message.fromStatusEffect
	});
};

BenchmarkInterface.prototype.OnGlobalTrainingFinished = function(message)
{
	this.Record("training_finished", { "owner": message.owner,
		"entities": message.entities.map(id => this.Facts(id)) });
};

BenchmarkInterface.prototype.OnGlobalResearchFinished = function(message)
{
	this.Record("research_finished", { "player": message.player, "technology": message.tech });
};

BenchmarkInterface.prototype.OnGlobalGarrisonedUnitsChanged = function(message)
{
	this.Record("garrison_changed", {
		"added": message.added.map(id => ({ ...this.Facts(id),
			"holder": Engine.QueryInterface(id, IID_Garrisonable)?.HolderID() ?? null })),
		"removed": message.removed.map(id => this.Facts(id))
	});
};

BenchmarkInterface.prototype.OnGlobalPlayerWon = function(message)
{
	this.Record("player_won", { "player": message.playerId });
};

BenchmarkInterface.prototype.OnGlobalPlayerDefeated = function(message)
{
	this.Record("player_defeated", { "player": message.playerId });
};

/** Per-turn integrals for idle/production diagnostics. Eligibility is gathering capability. */
BenchmarkInterface.prototype.AccumulateMetrics = function()
{
	if (!this.TelemetryEnabled())
		return;
	const count = Engine.QueryInterface(SYSTEM_ENTITY, IID_PlayerManager).GetNumPlayers();
	const metrics = player =>
	{
		if (!this.intervalMetrics.has(player))
			this.intervalMetrics.set(player, { "turns": 0, "worker_turns": 0, "idle_worker_turns": 0,
				"gathering_worker_turns": 0, "combat_worker_turns": 0, "producer_turns": 0,
				"active_producer_turns": 0, "blocked_producer_turns": 0, "empty_producer_turns": 0 });
		return this.intervalMetrics.get(player);
	};
	for (let player = 1; player < count; ++player)
		++metrics(player).turns;
	for (const id of Engine.GetEntitiesWithInterface(IID_ResourceGatherer))
	{
		const owner = Engine.QueryInterface(id, IID_Ownership)?.GetOwner() ?? 0;
		const unitAI = Engine.QueryInterface(id, IID_UnitAI);
		if (owner < 1 || !unitAI || Engine.QueryInterface(id, IID_Mirage))
			continue;
		const record = metrics(owner);
		const state = unitAI.GetCurrentState();
		++record.worker_turns;
		if (unitAI.IsIdle())
			++record.idle_worker_turns;
		else if (state.includes(".GATHER") || state.includes(".RETURNRESOURCE"))
			++record.gathering_worker_turns;
		else if (state.includes(".COMBAT"))
			++record.combat_worker_turns;
	}
	for (const id of Engine.GetEntitiesWithInterface(IID_ProductionQueue))
	{
		const owner = Engine.QueryInterface(id, IID_Ownership)?.GetOwner() ?? 0;
		if (owner < 1 || Engine.QueryInterface(id, IID_Foundation) || Engine.QueryInterface(id, IID_Mirage))
			continue;
		const record = metrics(owner);
		const queue = Engine.QueryInterface(id, IID_ProductionQueue).GetQueue();
		++record.producer_turns;
		if (!queue.length)
			++record.empty_producer_turns;
		else if (queue[0].paused)
			++record.blocked_producer_turns;
		else
			++record.active_producer_turns;
	}
};

Engine.RegisterSystemComponentType(IID_BenchmarkInterface, "BenchmarkInterface", BenchmarkInterface);
