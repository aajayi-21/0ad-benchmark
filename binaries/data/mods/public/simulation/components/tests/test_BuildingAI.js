Engine.LoadHelperScript("Player.js");
Engine.LoadHelperScript("Position.js");
Engine.LoadHelperScript("Sound.js");
Engine.LoadHelperScript("ValueModification.js");
Engine.LoadComponentScript("interfaces/Auras.js");
Engine.LoadComponentScript("interfaces/BuildingAI.js");
Engine.LoadComponentScript("interfaces/ModifiersManager.js");
Engine.LoadComponentScript("interfaces/Timer.js");
Engine.LoadComponentScript("interfaces/UnitAI.js");
Engine.LoadComponentScript("BuildingAI.js");

/**
 * A target being in the range query, or handed to us directly by UnitAI as a
 * focus target, is not enough to shoot it: neither is guaranteed to be within
 * the actual parabolic trajectory (the query's flat base range ignores
 * elevation, and a UnitAI target never goes through the query at all).
 * PerformAttack does no range check of its own and damage lands regardless,
 * so FireArrows has to be the one to check.
 */
function TestFireArrowsChecksRange()
{
	ResetState();

	const tower = 10;
	const targetA = 20;
	const targetB = 21;

	const attacked = [];
	let inParabolicRange = {};

	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": () => 1,
		"CancelTimer": () => {}
	});

	AddMock(SYSTEM_ENTITY, IID_ObstructionManager, {
		"IsInTargetParabolicRange": (ent, target) => inParabolicRange[target] ?? true
	});

	AddMock(tower, IID_Attack, {
		"GetRange": () => ({ "min": 10, "max": 60 }),
		"GetAttackYOrigin": () => 12,
		"GetTimers": () => ({ "prepare": 0, "repeat": 2000 }),
		"GetPreference": target => target == targetA ? 0 : 1,
		"CanAttack": () => true,
		"PerformAttack": (type, ent) => attacked.push(ent)
	});

	const buildingAI = ConstructComponent(tower, "BuildingAI", {
		"DefaultArrowCount": "4",
		"GarrisonArrowMultiplier": "1",
		"GarrisonArrowClasses": "Infantry"
	});

	const fire = function()
	{
		attacked.length = 0;
		buildingAI.currentRound = 0;
		buildingAI.FireArrows();
	};

	// Without a focus target: a target reported by the range query, but not
	// actually in range, is not fired on...
	buildingAI.targetUnits = [targetA];
	inParabolicRange = { [targetA]: false };
	fire();
	TS_ASSERT_EQUALS(attacked.length, 0);

	// ...but once genuinely in range, it is.
	inParabolicRange = { [targetA]: true };
	fire();
	TS_ASSERT_UNEVAL_EQUALS(attacked, [targetA]);

	// A target out of range is skipped in favour of the next preferred one
	// that is in range, rather than blocking fire entirely.
	buildingAI.targetUnits = [targetA, targetB];
	inParabolicRange = { [targetA]: false, [targetB]: true };
	fire();
	TS_ASSERT_UNEVAL_EQUALS(attacked, [targetB]);

	// With a focus target (set by UnitAI, e.g. via an attack-move order):
	// same story, even though it never went through the range query.
	buildingAI.targetUnits = [];
	buildingAI.unitAITarget = targetA;
	inParabolicRange = { [targetA]: false };
	fire();
	TS_ASSERT_EQUALS(attacked.length, 0);

	inParabolicRange = { [targetA]: true };
	fire();
	TS_ASSERT_UNEVAL_EQUALS(attacked, [targetA]);
}

TestFireArrowsChecksRange();
