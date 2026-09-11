function BenchmarkInterface() {}

BenchmarkInterface.prototype.Schema = "<a:component type='system'/><empty/>";

// Observation bookkeeping must not participate in simulation serialization or replay hashes.
BenchmarkInterface.prototype.Serialize = null;
BenchmarkInterface.prototype.Init = function()
{
	this.handles = new Map();
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
				handles.set(entity.id, "own-" + (handles.size + 1));
		views[seat] = {
			"seat": seat,
			"coverage": "owned_only_m1",
			"unavailable_sections": ["visible_entities", "last_seen", "map", "events", "orders"],
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
				"foundation_progress": entity.foundation?.progress ?? null,
				"holder": ownIDs.has(entity.holder) ? handles.get(entity.holder) : null,
				"garrisoned": entity.garrisoned.filter(id => ownIDs.has(id)).map(id => handles.get(id)),
				"queue": entity.queue.map(item => ({
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
		"evaluator": { "players": players, "entities": entities }
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

Engine.RegisterSystemComponentType(IID_BenchmarkInterface, "BenchmarkInterface", BenchmarkInterface);
