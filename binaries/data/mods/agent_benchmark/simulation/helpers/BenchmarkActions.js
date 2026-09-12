class BenchmarkActionError extends Error {}

/** Narrow player actions. Native commands are constructed here, never copied from callers. */
class BenchmarkActions
{
	static Reject(code)
	{
		throw new BenchmarkActionError(code);
	}

	static IsRejection(error)
	{
		return error instanceof BenchmarkActionError;
	}

	static Object(value, keys)
	{
		if (!value || typeof value != "object" || Array.isArray(value) ||
			Object.keys(value).some(key => !keys.includes(key)))
			this.Reject("invalid_schema");
	}

	static Identifier(value)
	{
		return typeof value == "string" && /^[a-zA-Z0-9_-]{1,80}$/.test(value);
	}

	static Position(value)
	{
		this.Object(value, ["x", "z"]);
		const size = Engine.QueryInterface(SYSTEM_ENTITY, IID_Terrain).GetMapSize();
		if (![value.x, value.z].every(n => Number.isFinite(n) && n >= 0 && n < size))
			this.Reject("invalid_position");
		return { "x": value.x, "z": value.z };
	}

	static Owned(seat, id)
	{
		return !Engine.QueryInterface(id, IID_Mirage) && Engine.QueryInterface(id, IID_Ownership)?.GetOwner() == seat;
	}

	static Visible(seat, id)
	{
		return !Engine.QueryInterface(id, IID_Mirage) &&
			Engine.QueryInterface(SYSTEM_ENTITY, IID_RangeManager).GetLosVisibility(id, seat) == "visible";
	}

	static Translate(component, seat, action)
	{
		this.Object(action, ["action_id", "type", "units", "position", "queued", "target",
			"allow_capture", "template", "angle", "autorepair", "autocontinue", "building",
			"count", "technology", "queue", "stance", "holder"]);
		if (!this.Identifier(action.action_id) || typeof action.type != "string")
			this.Reject("invalid_schema");
		const fields = {
			"wait": [], "resign": [],
			"move": ["units", "position", "queued"],
			"attack_move": ["units", "position", "queued", "allow_capture"],
			"attack": ["units", "target", "queued", "allow_capture"],
			"gather": ["units", "target", "queued"],
			"return_resources": ["units", "target", "queued"],
			"repair": ["units", "target", "queued", "autocontinue"],
			"build": ["units", "template", "position", "angle", "queued", "autorepair", "autocontinue"],
			"train": ["building", "template", "count"],
			"research": ["building", "technology"],
			"cancel_production": ["building", "queue"],
			"set_rally_point": ["building", "position", "queued"],
			"stop": ["units", "queued"], "stance": ["units", "stance"],
			"garrison": ["units", "holder", "queued"], "unload": ["units", "holder"]
		};
		if (!Object.hasOwn(fields, action.type))
			this.Reject("unsupported_action");
		this.Object(action, ["action_id", "type", ...fields[action.type]]);
		const resolve = (handle, owned = true) =>
		{
			const id = component.ResolveHandle(seat, handle);
			if (!id || (owned ? !this.Owned(seat, id) : !this.Visible(seat, id)))
				this.Reject("unavailable_entity");
			return id;
		};
		const command = { "type": action.type };
		if (fields[action.type].includes("units"))
		{
			if (!Array.isArray(action.units) || !action.units.length || action.units.length > 64 ||
				new Set(action.units).size != action.units.length)
				this.Reject("invalid_group");
			command.entities = action.units.map(handle => resolve(handle));
			// Formation control is a later action family; keep each order on the named units.
			command.formation = NULL_FORMATION;
		}
		if (fields[action.type].includes("building"))
			command.entity = resolve(action.building);
		if (fields[action.type].includes("target"))
			command.target = resolve(action.target, !["attack", "gather"].includes(action.type));
		if (fields[action.type].includes("holder"))
			command.target = resolve(action.holder);
		if (fields[action.type].includes("position"))
			Object.assign(command, this.Position(action.position));
		for (const [external, internal] of [["queued", "queued"], ["allow_capture", "allowCapture"],
			["autorepair", "autorepair"], ["autocontinue", "autocontinue"]])
			if (fields[action.type].includes(external))
			{
				if (typeof action[external] != "boolean")
					this.Reject("invalid_schema");
				command[internal] = action[external];
			}
		switch (action.type)
		{
		case "wait": return null;
		case "move": command.type = "walk"; break;
		case "attack_move": command.type = "attack-walk"; break;
		case "return_resources": command.type = "returnresource"; break;
		case "build":
			command.type = "construct";
			command.template = action.template;
			if (!Number.isFinite(action.angle) || Math.abs(action.angle) > 2 * Math.PI)
				this.Reject("invalid_angle");
			command.angle = action.angle;
			break;
		case "train":
			if (!Number.isInteger(action.count) || action.count < 1 || action.count > 5)
				this.Reject("invalid_count");
			command.entities = [command.entity];
			delete command.entity;
			command.template = action.template;
			command.count = action.count;
			break;
		case "research": command.template = action.technology; break;
		case "cancel_production":
			command.type = "stop-production";
			command.id = component.ResolveQueue(seat, command.entity, action.queue);
			if (command.id === undefined)
				this.Reject("unavailable_queue");
			break;
		case "set_rally_point":
			command.type = "set-rallypoint";
			command.structures = [command.entity];
			delete command.entity;
			command.data = { "command": "walk" };
			break;
		case "stance":
			if (!["violent", "aggressive", "defensive", "passive", "standground"].includes(action.stance))
				this.Reject("invalid_stance");
			command.name = action.stance;
			break;
		case "unload": command.garrisonHolder = command.target; delete command.target; break;
		default: break;
		}
		this.CheckShared(seat, command);
		for (const id of command.entities ?? [])
			if (!this.CanApply(seat, id, command))
				this.Reject("unavailable_entity");
		return command;
	}

	/** Validate live authority and public capabilities again immediately before execution. */
	static CheckShared(seat, command)
	{
		if (QueryPlayerIDInterface(seat).IsAI())
			this.Reject("builtin_ai_seat");
		if (QueryPlayerIDInterface(seat).GetState() != "active")
			this.Reject("inactive_seat");
		const target = command.target ?? command.garrisonHolder;
		if (target !== undefined)
		{
			if (["attack", "gather"].includes(command.type))
			{
				if (!this.Visible(seat, target))
					this.Reject("unavailable_entity");
			}
			else if (!this.Owned(seat, target))
				this.Reject("unavailable_entity");
		}
		for (const id of [command.entity, ...(command.structures ?? [])].filter(entity => entity !== undefined))
			if (!this.Owned(seat, id))
				this.Reject("unavailable_entity");
		const technology = QueryPlayerIDInterface(seat, IID_TechnologyManager);
		if (["train", "construct"].includes(command.type))
		{
			const manager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
			if (typeof command.template != "string" || !/^[a-z0-9_/-]{1,200}$/.test(command.template) ||
				!manager.TemplateExists(command.template) ||
				(command.type == "construct" && manager.GetTemplate(command.template).WallSet))
				this.Reject("unavailable_template");
			if (!technology.CanProduce(command.template))
				this.Reject("requirements_unmet");
		}
		if (command.type == "research")
		{
			const list = Engine.QueryInterface(command.entity, IID_Researcher)?.GetTechnologiesList() ?? [];
			if (!list.some(tech => tech === command.template || tech?.pair?.includes(command.template)) ||
				!technology.CanResearch(command.template))
				this.Reject("unavailable_technology");
		}
		if (command.type == "stop-production" &&
			!Engine.QueryInterface(command.entity, IID_ProductionQueue)?.GetQueue().some(item => item.id == command.id))
			this.Reject("unavailable_queue");
		if (command.type == "set-rallypoint" && !Engine.QueryInterface(command.structures[0], IID_RallyPoint))
			this.Reject("unavailable_entity");
	}

	/** Templates `id` can place with a plain construct command; wall sets need wall placement. */
	static Buildable(id)
	{
		const manager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
		return (Engine.QueryInterface(id, IID_Builder)?.GetEntitiesList() ?? [])
			.filter(name => !manager.GetTemplate(name)?.WallSet);
	}

	static CanApply(seat, id, command)
	{
		if (!this.Owned(seat, id))
			return false;
		if (command.type == "train")
			return !!Engine.QueryInterface(id, IID_Trainer)?.CanTrain(command.template);
		const unit = Engine.QueryInterface(id, IID_UnitAI);
		if (!unit)
			return false;
		switch (command.type)
		{
		case "construct": return this.Buildable(id).includes(command.template);
		case "attack": return unit.CanAttack(command.target);
		case "gather": return unit.CanGather(command.target);
		case "returnresource": return unit.CanReturnResource(command.target, true);
		case "repair": return unit.CanRepair(command.target);
		case "garrison": return !Engine.QueryInterface(id, IID_Garrisonable)?.HolderID() && unit.CanGarrison(command.target);
		case "unload": return Engine.QueryInterface(id, IID_Garrisonable)?.HolderID() == command.garrisonHolder;
		case "stance": return unit.GetSelectableStances().includes(command.name) && !unit.IsTurret();
		case "walk":
		case "attack-walk": return unit.AbleToMove() && !Engine.QueryInterface(id, IID_Garrisonable)?.HolderID();
		default: return true;
		}
	}
}

Engine.RegisterGlobal("BenchmarkActions", BenchmarkActions);
