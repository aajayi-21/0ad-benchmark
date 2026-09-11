// Replayable fixture intervention: one damaged owned storehouse, passive starting units.
for (const id of Engine.GetEntitiesWithInterface(IID_UnitAI))
	Engine.QueryInterface(id, IID_UnitAI).SwitchToStance("passive");
for (const id of Engine.GetEntitiesWithInterface(IID_Health))
	if (Engine.QueryInterface(id, IID_Ownership)?.GetOwner() == 1 &&
		Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id) == "structures/athen/storehouse")
	{
		const health = Engine.QueryInterface(id, IID_Health);
		health.SetHitpoints(health.GetMaxHitpoints() / 2);
	}
