// Deterministic cavalry raid on the workers at turn 100; surviving raiders are removed at turn
// 400 so the remainder of the episode measures recovery, not defence.
Trigger.prototype.SuiteRecoveryTick = function()
{
	const turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200);
	const manager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
	const owned = (seat, suffix) => Engine.GetEntitiesWithInterface(IID_Ownership).filter(id =>
		!Engine.QueryInterface(id, IID_Mirage) && Engine.QueryInterface(id, IID_Ownership).GetOwner() == seat &&
		manager.GetCurrentTemplateName(id).endsWith(suffix));
	if (turn == 100)
	{
		const workers = owned(1, "support_civilian");
		const raiders = owned(2, "cavalry_swordsman_b");
		if (workers.length && raiders.length)
		{
			const point = Engine.QueryInterface(workers[0], IID_Position).GetPosition2D();
			ProcessCommand(2, { "type": "attack-walk", "entities": raiders, "x": point.x, "z": point.y,
				"allowCapture": false, "queued": false, "formation": NULL_FORMATION });
		}
	}
	if (turn == 400)
		for (const id of owned(2, "cavalry_swordsman_b"))
			Engine.QueryInterface(id, IID_Health).Kill();
	if (turn < 400)
		this.DoAfterDelay(200, "SuiteRecoveryTick", {});
};

Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(200, "SuiteRecoveryTick", {});
