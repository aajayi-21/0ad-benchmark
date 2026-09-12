// Deterministic ram raid on player 1's civic centre: wave one (one ram, three swordsmen) attacks
// at turn 250; wave two (one more ram, four swordsmen) spawns at the staging point and attacks at
// turn 900. Idle raiders are re-ordered every 100 turns. Raider behaviour is scripted through
// ordinary commands; defender units keep their default stances.
Trigger.prototype.SuiteRaidWave = function()
{
	const turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200);
	const manager = Engine.QueryInterface(SYSTEM_ENTITY, IID_TemplateManager);
	const owned = (seat, suffix) => Engine.GetEntitiesWithInterface(IID_Ownership).filter(id =>
		!Engine.QueryInterface(id, IID_Mirage) && Engine.QueryInterface(id, IID_Ownership).GetOwner() == seat &&
		manager.GetCurrentTemplateName(id).endsWith(suffix));
	const centre = owned(1, "civil_centre")[0];
	if (!centre)
		return;
	const target = Engine.QueryInterface(centre, IID_Position).GetPosition2D();
	if (turn == 900)
	{
		// Wave two spawns at the map's staging point: 44 tiles from the defender's centre toward
		// the raider's own centre, outside vision and clear of every footprint.
		const enemyCentre = owned(2, "civil_centre")[0];
		const away = enemyCentre ? Engine.QueryInterface(enemyCentre, IID_Position).GetPosition2D() : target;
		const direction = Vector2D.sub(away, target).normalize();
		const anchor = Vector2D.add(target, direction.mult(176));
		const spawn = (name, dx, dz) =>
		{
			const id = Engine.AddEntity(name);
			Engine.QueryInterface(id, IID_Ownership).SetOwner(2);
			Engine.QueryInterface(id, IID_Position).JumpTo(anchor.x + dx, anchor.y + dz);
		};
		spawn("units/rome/siege_ram", 0, 8);
		for (let i = 0; i < 4; ++i)
			spawn("units/rome/infantry_swordsman_b", -6 + 4 * i, 12);
	}
	const rams = owned(2, "siege_ram").filter(id => Engine.QueryInterface(id, IID_UnitAI)?.IsIdle());
	if (rams.length)
		ProcessCommand(2, { "type": "attack", "entities": rams, "target": centre, "allowCapture": false,
			"queued": false, "formation": NULL_FORMATION });
	const escorts = owned(2, "infantry_swordsman_b").filter(id => Engine.QueryInterface(id, IID_UnitAI)?.IsIdle());
	if (escorts.length)
		ProcessCommand(2, { "type": "attack-walk", "entities": escorts, "x": target.x, "z": target.y,
			"allowCapture": false, "queued": false, "formation": NULL_FORMATION });
};

Trigger.prototype.SuiteRaidTick = function()
{
	const turn = Math.round(Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime() / 200);
	if (turn == 250 || turn == 900 || (turn > 250 && turn % 100 == 0))
		this.SuiteRaidWave();
	if (turn < 1800)
		this.DoAfterDelay(200, "SuiteRaidTick", {});
};

Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(200, "SuiteRaidTick", {});
