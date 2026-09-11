// All interventions are pinned in replay attributes and occur on simulation timer boundaries.
// The variant and entity IDs below are evaluator-only fixture data.
Trigger.prototype.M3Tick = function()
{
	const turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200);
	const mode = InitAttributes.settings.M3Mode ?? "memory";
	const variant = InitAttributes.settings.M3Variant ?? 0;
	const find = (seat, suffix) => Engine.GetEntitiesWithInterface(IID_Ownership).filter(id =>
		!Engine.QueryInterface(id, IID_Mirage) && Engine.QueryInterface(id, IID_Ownership).GetOwner() == seat &&
		Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id).endsWith(suffix));
	const move = (id, x, z) => Engine.QueryInterface(id, IID_Position).JumpTo(x, z);
	const spawn = (name, x, z) =>
	{
		const id = Engine.AddEntity(name);
		Engine.QueryInterface(id, IID_Ownership).SetOwner(0);
		move(id, x, z);
	};
	if (mode == "memory")
	{
		if (turn == 2)
			move(find(1, "cavalry_swordsman_b")[0], 160, 280);
		if (turn == 3)
		{
			const enemy = find(2, "infantry_spearman_b")[0];
			move(enemy, 420, 400);
			if (variant == 1)
			{
				Engine.QueryInterface(enemy, IID_Health).SetHitpoints(23);
				ProcessCommand(2, { "type": "walk", "entities": [enemy], "x": 440, "z": 410,
					"queued": false, "formation": NULL_FORMATION });
				QueryPlayerIDInterface(2).SetResourceCounts({ "food": 4321, "wood": 1234, "stone": 456, "metal": 678 });
				QueryPlayerIDInterface(2, IID_TechnologyManager).ResearchTechnology("phase_town");
				Engine.QueryInterface(find(0, "tree/oak")[0], IID_ResourceSupply).TakeResources(17);
				for (let i = 0; i < 7; ++i)
					spawn("gaia/tree/oak", 400 + i, 440);
				ProcessCommand(2, { "type": "train", "entities": find(2, "civil_centre"),
					"template": "units/athen/support_civilian", "count": 3 });
			}
			if (variant == 2)
				Engine.QueryInterface(enemy, IID_Health).Kill();
			if (variant == 3)
				ChangeEntityTemplate(enemy, "units/athen/infantry_spearman_a");
		}
		if (turn == 4)
			move(find(2, "infantry_javelineer_a")[0], 180, 280);
		if (turn == 5)
			move(find(2, "infantry_javelineer_a")[0], 420, 420);
		if (turn == 6)
			spawn("gaia/fruit/berry_01", 160, 300);
		if (turn == 10)
			move(find(1, "cavalry_swordsman_b")[0], 200, 200);
		if (turn == 12 && variant != 2)
			move(find(2, variant == 3 ? "infantry_spearman_a" : "infantry_spearman_b")[0], 248, 200);
	}
	if (mode == "privacy" && turn == 1 && variant == 1)
	{
		QueryPlayerIDInterface(2).SetResourceCounts({ "food": 9876, "wood": 6789, "stone": 789, "metal": 987 });
		QueryPlayerIDInterface(2, IID_TechnologyManager).ResearchTechnology("gather_capacity_basket");
		Engine.QueryInterface(find(2, "infantry_javelineer_a")[0], IID_Garrisonable).Garrison(find(2, "civil_centre")[0]);
		ProcessCommand(2, { "type": "train", "entities": find(2, "civil_centre"),
			"template": "units/athen/support_civilian", "count": 3 });
	}
	if (mode == "queue_history")
	{
		const producer = find(2, "civil_centre")[0];
		if (turn == 1 && variant == 1)
			for (let i = 0; i < 10; ++i)
			{
				ProcessCommand(2, { "type": "train", "entities": [producer],
					"template": "units/athen/support_civilian", "count": 1 });
				ProcessCommand(2, { "type": "stop-production", "entity": producer,
					"id": Engine.QueryInterface(producer, IID_ProductionQueue).GetQueue()[0].id });
			}
		if (turn == 2)
			Engine.QueryInterface(producer, IID_Ownership).SetOwner(1);
	}
	if (mode == "lifecycle")
	{
		if (turn == 2)
		{
			ChangeEntityTemplate(find(1, "cavalry_swordsman_b")[0], "units/athen/cavalry_swordsman_a");
			ChangeEntityTemplate(find(2, "infantry_spearman_b")[0], "units/athen/infantry_spearman_a");
		}
		if (turn == 3)
		{
			Engine.QueryInterface(find(1, "support_civilian")[0], IID_Garrisonable).Garrison(find(1, "civil_centre")[0]);
			Engine.QueryInterface(find(2, "house")[0], IID_Ownership).SetOwner(1);
		}
		if (turn == 4)
			Engine.QueryInterface(find(2, "infantry_spearman_a")[0], IID_Health).Kill();
	}
	if (turn < 12)
		this.DoAfterDelay(200, "M3Tick", {});
};

for (const id of Engine.GetEntitiesWithInterface(IID_UnitAI))
	Engine.QueryInterface(id, IID_UnitAI).SetStance("passive");
if (["privacy", "queue_history"].includes(InitAttributes.settings.M3Mode))
	for (const id of Engine.GetEntitiesWithInterface(IID_UnitAI))
		if (Engine.QueryInterface(id, IID_Ownership).GetOwner() == 1 &&
			Engine.QueryInterface(id, IID_Identity).GetClassesList().includes("Cavalry"))
			Engine.QueryInterface(id, IID_Position).JumpTo(328, 328);
Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(200, "M3Tick", {});
