// Deterministic raid: pinned in the replay attributes, driven by simulation timers only.
// Civilians stand their ground so the raid produces losses; spearmen defend; leftover raiders
// are removed by script so the trace ends inside the fixture horizon.
Trigger.prototype.M4Raid = function()
{
	const turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200);
	const owned = (seat, suffix) => Engine.GetEntitiesWithInterface(IID_Ownership).filter(id =>
		!Engine.QueryInterface(id, IID_Mirage) && Engine.QueryInterface(id, IID_Ownership).GetOwner() == seat &&
		Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager).GetCurrentTemplateName(id).endsWith(suffix));
	if (turn == 20)
		ProcessCommand(2, { "type": "attack-walk", "entities": owned(2, "cavalry_swordsman_b"),
			"x": 168, "z": 152, "allowCapture": false, "queued": false,
			"formation": NULL_FORMATION });
	if (turn == 200)
		for (const id of owned(2, "cavalry_swordsman_b"))
			Engine.QueryInterface(id, IID_Health).Kill();
	if (turn < 200)
		this.DoAfterDelay(200, "M4Raid", {});
};

for (const id of Engine.GetEntitiesWithInterface(IID_UnitAI))
{
	const owner = Engine.QueryInterface(id, IID_Ownership).GetOwner();
	const classes = Engine.QueryInterface(id, IID_Identity).GetClassesList();
	if (owner == 1 && classes.includes("Infantry"))
		Engine.QueryInterface(id, IID_UnitAI).SwitchToStance("defensive");
	else
		Engine.QueryInterface(id, IID_UnitAI).SwitchToStance(owner == 1 ? "standground" : "aggressive");
}
Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(200, "M4Raid", {});
