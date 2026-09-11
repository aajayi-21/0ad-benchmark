// Exercise the real component repeatedly while Petra is active, before the next AI update.
Trigger.prototype.M1SnapshotStress = function()
{
	const component = Engine.QueryInterface(SYSTEM_ENTITY, IID_BenchmarkInterface);
	const ai = Engine.QueryInterface(SYSTEM_ENTITY, IID_AIInterface);
	const bookkeeping = () => JSON.stringify({
		"events": ai.events,
		"changedEntities": ai.changedEntities,
		"changedTemplateInfo": ai.changedTemplateInfo,
		"changedEntityTemplateInfo": ai.changedEntityTemplateInfo,
		"proxies": Engine.GetEntitiesWithInterface(IID_AIProxy).map(id =>
		{
			const proxy = Engine.QueryInterface(id, IID_AIProxy);
			return [id, proxy.needsFullGet, proxy.changes];
		})
	});
	const before = bookkeeping();
	if (Object.values(ai.events).some(events => events.length))
		print("M1_PENDING_EVENTS\n");
	if (Object.keys(ai.changedEntities).length)
		print("M1_PENDING_ENTITIES\n");
	const first = component.GetSnapshot([1, 2]);
	const second = component.GetSnapshot([1, 2]);
	const catalog = component.GetCatalog(1, ["structures/spart/house"], ["phase_town"]);
	if (JSON.stringify(catalog) != JSON.stringify(component.GetCatalog(1, ["structures/spart/house"], ["phase_town"])))
		throw new Error("M1 catalog differed on repeated reads");
	if (JSON.stringify(first) != JSON.stringify(second) || before != bookkeeping())
		throw new Error("M1 snapshot changed AI bookkeeping or differed on repeated reads");
	// Returned arrays must be detached from the component's mutable data.
	for (const entity of first.evaluator.entities)
	{
		entity.orders.length = 0;
		entity.queue.length = 0;
		entity.garrisoned.length = 0;
	}
	if (JSON.stringify(second) != JSON.stringify(component.GetSnapshot([1, 2])))
		throw new Error("M1 snapshot exposed mutable simulation data");
	if (Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() % 20000 == 0)
		print("M1_STRESS verified\n");
	this.DoAfterDelay(200, "M1SnapshotStress", {});
};

Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(0, "M1SnapshotStress", {});
