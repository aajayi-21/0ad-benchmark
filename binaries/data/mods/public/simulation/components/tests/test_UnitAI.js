Engine.LoadHelperScript("Player.js");
Engine.LoadHelperScript("Position.js");
Engine.LoadHelperScript("Sound.js");
Engine.LoadComponentScript("interfaces/Auras.js");
Engine.LoadComponentScript("interfaces/Builder.js");
Engine.LoadComponentScript("interfaces/BuildingAI.js");
Engine.LoadComponentScript("interfaces/Capturable.js");
Engine.LoadComponentScript("interfaces/Diplomacy.js");
Engine.LoadComponentScript("interfaces/Garrisonable.js");
Engine.LoadComponentScript("interfaces/Resistance.js");
Engine.LoadComponentScript("interfaces/Formation.js");
Engine.LoadComponentScript("interfaces/Heal.js");
Engine.LoadComponentScript("interfaces/Health.js");
Engine.LoadComponentScript("interfaces/Pack.js");
Engine.LoadComponentScript("interfaces/ResourceSupply.js");
Engine.LoadComponentScript("interfaces/ResourceGatherer.js");
Engine.LoadComponentScript("interfaces/Timer.js");
Engine.LoadComponentScript("interfaces/Turretable.js");
Engine.LoadComponentScript("interfaces/UnitAI.js");
Engine.LoadComponentScript("Formation.js");
Engine.LoadComponentScript("UnitAI.js");

/**
 * Fairly straightforward test that entity renaming is handled
 * by unitAI states. These ought to be augmented with integration tests, ideally.
 */
function TestTargetEntityRenaming(init_state, post_state, setup)
{
	ResetState();
	const player_ent = 5;
	const target_ent = 6;

	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": () => {},
		"SetTimeout": () => {}
	});
	AddMock(SYSTEM_ENTITY, IID_ObstructionManager, {
		"IsInTargetRange": () => false
	});

	const unitAI = ConstructComponent(player_ent, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive",
		"FleeDistance": 10
	});
	unitAI.OnCreate();

	setup(unitAI, player_ent, target_ent);

	TS_ASSERT_EQUALS(unitAI.GetCurrentState(), init_state);

	unitAI.OnGlobalEntityRenamed({
		"entity": target_ent,
		"newentity": target_ent + 1
	});

	TS_ASSERT_EQUALS(unitAI.GetCurrentState(), post_state);
}

TestTargetEntityRenaming(
	"INDIVIDUAL.GARRISON.APPROACHING", "INDIVIDUAL.IDLE",
	(unitAI, player_ent, target_ent) =>
	{
		unitAI.CanGarrison = (target) => target == target_ent;
		unitAI.MoveToTargetRange = (target) => target == target_ent;
		unitAI.AbleToMove = () => true;

		unitAI.Garrison(target_ent, false);
	}
);

TestTargetEntityRenaming(
	"INDIVIDUAL.REPAIR.REPAIRING", "INDIVIDUAL.REPAIR.REPAIRING",
	(unitAI, player_ent, target_ent) =>
	{

		AddMock(player_ent, IID_Builder, {
			"StartRepairing": () => true,
			"StopRepairing": () => {}
		});

		QueryBuilderListInterface = () => {};
		unitAI.CheckTargetRange = () => true;
		unitAI.CanRepair = (target) => target == target_ent;

		unitAI.Repair(target_ent, false, false);
	}
);


TestTargetEntityRenaming(
	"INDIVIDUAL.FLEEING", "INDIVIDUAL.FLEEING",
	(unitAI, player_ent, target_ent) =>
	{
		PositionHelper.DistanceBetweenEntities = () => 10;
		unitAI.CheckTargetRangeExplicit = () => false;

		AddMock(player_ent, IID_UnitMotion, {
			"MoveToTargetRange": () => true,
			"GetRunMultiplier": () => 1,
			"SetSpeedMultiplier": () => {},
			"GetAcceleration": () => 1,
			"StopMoving": () => {}
		});

		unitAI.Flee(target_ent, false);
	}
);

/* Regression test.
 * Tests the FSM behaviour of a unit when walking as part of a formation,
 * then exiting the formation.
 * mode == 0: There is no enemy unit nearby.
 * mode == 1: There is a live enemy unit nearby.
 * mode == 2: There is a dead enemy unit nearby.
 */
function TestFormationExiting(mode)
{
	ResetState();

	var playerEntity = 5;
	var unit = 10;
	var enemy = 20;
	var controller = 30;


	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": function() { },
		"SetTimeout": function() { },
	});

	AddMock(SYSTEM_ENTITY, IID_RangeManager, {
		"CreateActiveQuery": function(ent, minRange, maxRange, players, iid, flags, accountForSize)
		{
			return 1;
		},
		"EnableActiveQuery": function(id) { },
		"ResetActiveQuery": function(id) { if (mode == 0) return []; return [enemy]; },
		"DisableActiveQuery": function(id) { },
		"GetEntityFlagMask": function(identifier) { },
		"GetLosVisibility": (ent, player) => "visible"
	});

	AddMock(SYSTEM_ENTITY, IID_TemplateManager, {
		"GetCurrentTemplateName": function(ent) { return "special/formations/line_closed"; },
	});

	AddMock(SYSTEM_ENTITY, IID_PlayerManager, {
		"GetPlayerByID": function(id) { return playerEntity; },
		"GetNumPlayers": function() { return 2; },
	});

	AddMock(playerEntity, IID_Diplomacy, {
		"IsAlly": function() { return false; },
		"IsEnemy": function() { return true; },
		"GetEnemies": function() { return [2]; },
	});

	var unitAI = ConstructComponent(unit, "UnitAI", { "FormationController": "false", "DefaultStance": "aggressive" });

	AddMock(unit, IID_Identity, {
		"GetClassesList": function() { return []; },
	});

	AddMock(unit, IID_Ownership, {
		"GetOwner": function() { return 1; },
	});

	AddMock(unit, IID_Position, {
		"GetTurretParent": function() { return INVALID_ENTITY; },
		"GetPosition": function() { return new Vector3D(); },
		"GetPosition2D": function() { return new Vector2D(); },
		"GetRotation": function() { return { "y": 0 }; },
		"IsInWorld": function() { return true; },
	});

	AddMock(unit, IID_UnitMotion, {
		"GetWalkSpeed": () => 1,
		"GetAcceleration": () => 1,
		"SetSpeedMultiplier": () => {},
		"MoveToFormationOffset": (target, x, z) => {},
		"MoveToTargetRange": (target, min, max) => true,
		"PossiblyAtDestination": () => false,
		"SetMemberOfFormation": () => {},
		"StopMoving": () => {},
		"SetFacePointAfterMove": () => {},
		"GetFacePointAfterMove": () => true,
		"GetPassabilityClassName": () => "default"
	});

	AddMock(unit, IID_Vision, {
		"GetRange": function() { return 10; },
	});

	AddMock(unit, IID_Attack, {
		"GetRange": function() { return { "max": 10, "min": 0 }; },
		"GetFullAttackRange": function() { return { "max": 40, "min": 0 }; },
		"GetBestAttackAgainst": function(t) { return "melee"; },
		"GetPreference": function(t) { return 0; },
		"GetTimers": function() { return { "prepare": 500, "repeat": 1000 }; },
		"CanAttack": function(v) { return true; },
		"CompareEntitiesByPreference": function(a, b) { return 0; },
		"IsTargetInRange": () => true,
		"StartAttacking": () => true
	});

	unitAI.OnCreate();

	unitAI.SetupAttackRangeQuery(1);


	if (mode == 1)
	{
		AddMock(enemy, IID_Health, {
			"GetHitpoints": function() { return 10; },
		});
		AddMock(enemy, IID_UnitAI, {
			"IsAnimal": () => "false",
			"IsDangerousAnimal": () => "false"
		});
	}
	else if (mode == 2)
		AddMock(enemy, IID_Health, {
			"GetHitpoints": function() { return 0; },
		});

	const controllerFormation = ConstructComponent(controller, "Formation", {
		"FormationShape": "square",
		"ShiftRows": "false",
		"SortingClasses": "",
		"WidthDepthRatio": 1,
		"UnitSeparationWidthMultiplier": 1,
		"UnitSeparationDepthMultiplier": 1,
		"SpeedMultiplier": 1,
		"Sloppiness": 0
	});
	const controllerAI = ConstructComponent(controller, "UnitAI", {
		"FormationController": "true",
		"DefaultStance": "aggressive"
	});

	AddMock(controller, IID_Position, {
		"JumpTo": function(x, z) { this.x = x; this.z = z; },
		"TurnTo": function() {},
		"GetTurretParent": function() { return INVALID_ENTITY; },
		"GetPosition": function() { return new Vector3D(this.x, 0, this.z); },
		"GetPosition2D": function() { return new Vector2D(this.x, this.z); },
		"GetRotation": function() { return { "y": 0 }; },
		"IsInWorld": function() { return true; },
		"MoveOutOfWorld": () => {}
	});

	AddMock(controller, IID_UnitMotion, {
		"GetWalkSpeed": () => 1,
		"StopMoving": () => {},
		"SetSpeedMultiplier": () => {},
		"SetAcceleration": (accel) => {},
		"SetPassabilityClassName": (name) => {},
		"MoveToPointRange": () => true,
		"SetFacePointAfterMove": () => {},
		"GetFacePointAfterMove": () => true,
		"GetPassabilityClassName": () => "default"
	});

	AddMock(SYSTEM_ENTITY, IID_Pathfinder, {
		"GetClearance": () => 1,
		"GetPassabilityClass": () => 16
	});

	controllerAI.OnCreate();


	TS_ASSERT_EQUALS(controllerAI.fsmStateName, "FORMATIONCONTROLLER.IDLE");
	TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.IDLE");

	controllerFormation.SetMembers([unit]);
	controllerAI.Walk(100, 100, false);

	TS_ASSERT_EQUALS(controllerAI.fsmStateName, "FORMATIONCONTROLLER.WALKING");
	TS_ASSERT_EQUALS(unitAI.fsmStateName, "FORMATIONMEMBER.WALKING");

	controllerFormation.Disband();

	unitAI.UnitFsm.ProcessMessage(unitAI, { "type": "Timer" });

	if (mode == 0)
		TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.IDLE");
	else if (mode == 1)
		TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.COMBAT.ATTACKING");
	else if (mode == 2)
		TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.IDLE");
	else
		TS_FAIL("invalid mode");
}

function TestMoveIntoFormationWhileAttacking()
{
	ResetState();

	var playerEntity = 5;
	var controller = 10;
	var enemy = 20;
	var unit = 30;
	var units = [];
	var unitCount = 8;
	var unitAIs = [];

	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": function() { },
		"SetTimeout": function() { },
	});


	AddMock(SYSTEM_ENTITY, IID_RangeManager, {
		"CreateActiveQuery": function(ent, minRange, maxRange, players, iid, flags, accountForSize)
		{
			return 1;
		},
		"EnableActiveQuery": function(id) { },
		"ResetActiveQuery": function(id) { return [enemy]; },
		"DisableActiveQuery": function(id) { },
		"GetEntityFlagMask": function(identifier) { },
		"GetLosVisibility": (target, player) => "visible",
		"GetLosVisibilityPosition": (x, z, player) => "visible"
	});


	AddMock(SYSTEM_ENTITY, IID_TemplateManager, {
		"GetCurrentTemplateName": function(ent) { return "special/formations/line_closed"; },
	});

	AddMock(SYSTEM_ENTITY, IID_PlayerManager, {
		"GetPlayerByID": function(id) { return playerEntity; },
		"GetNumPlayers": function() { return 2; },
	});

	AddMock(SYSTEM_ENTITY, IID_ObstructionManager, {
		"IsInTargetRange": (ent, target, min, max) => true
	});

	AddMock(playerEntity, IID_Diplomacy, {
		"IsAlly": function() { return false; },
		"IsEnemy": function() { return true; },
		"GetEnemies": function() { return [2]; },
	});

	// create units
	for (var i = 0; i < unitCount; i++)
	{

		units.push(unit + i);

		var unitAI = ConstructComponent(unit + i, "UnitAI", { "FormationController": "false", "DefaultStance": "aggressive" });

		AddMock(unit + i, IID_Identity, {
			"GetClassesList": function() { return []; },
		});

		AddMock(unit + i, IID_Ownership, {
			"GetOwner": function() { return 1; },
		});

		AddMock(unit + i, IID_Position, {
			"GetTurretParent": function() { return INVALID_ENTITY; },
			"GetPosition": function() { return new Vector3D(); },
			"GetPosition2D": function() { return new Vector2D(); },
			"GetRotation": function() { return { "y": 0 }; },
			"IsInWorld": function() { return true; },
		});

		AddMock(unit + i, IID_UnitMotion, {
			"GetWalkSpeed": () => 1,
			"GetAcceleration": () => 1,
			"SetSpeedMultiplier": () => {},
			"MoveToFormationOffset": (target, x, z) => {},
			"MoveToTargetRange": (target, min, max) => true,
			"PossiblyAtDestination": () => false,
			"SetMemberOfFormation": () => {},
			"StopMoving": () => {},
			"SetFacePointAfterMove": () => {},
			"GetFacePointAfterMove": () => true,
			"GetPassabilityClassName": () => "default"
		});

		AddMock(unit + i, IID_Vision, {
			"GetRange": function() { return 10; },
		});

		AddMock(unit + i, IID_Attack, {
			"GetRange": function() { return { "max": 10, "min": 0 }; },
			"GetFullAttackRange": function() { return { "max": 40, "min": 0 }; },
			"GetBestAttackAgainst": function(t) { return "melee"; },
			"GetTimers": function() { return { "prepare": 500, "repeat": 1000 }; },
			"CanAttack": function(v) { return true; },
			"CompareEntitiesByPreference": function(a, b) { return 0; },
			"IsTargetInRange": () => true,
			"StartAttacking": () => true,
			"StopAttacking": () => {}
		});

		unitAI.OnCreate();

		unitAI.SetupAttackRangeQuery(1);

		unitAIs.push(unitAI);
	}

	// create enemy
	AddMock(enemy, IID_Health, {
		"GetHitpoints": function() { return 40; },
	});

	AddMock(enemy, IID_Ownership, {
		"GetOwner": () => 2 // Different player ID (enemy)
	});

	const controllerFormation = ConstructComponent(controller, "Formation", {
		"FormationShape": "square",
		"ShiftRows": "false",
		"SortingClasses": "",
		"WidthDepthRatio": 1,
		"UnitSeparationWidthMultiplier": 1,
		"UnitSeparationDepthMultiplier": 1,
		"SpeedMultiplier": 1,
		"Sloppiness": 0
	});
	const controllerAI = ConstructComponent(controller, "UnitAI", {
		"FormationController": "true",
		"DefaultStance": "aggressive"
	});

	AddMock(controller, IID_Ownership, {
		"GetOwner": () => 1
	});

	AddMock(controller, IID_Position, {
		"GetTurretParent": () => INVALID_ENTITY,
		"JumpTo": function(x, z) { this.x = x; this.z = z; },
		"TurnTo": function() {},
		"GetPosition": function(){ return new Vector3D(this.x, 0, this.z); },
		"GetPosition2D": function(){ return new Vector2D(this.x, this.z); },
		"GetRotation": () => ({ "y": 0 }),
		"IsInWorld": () => true,
		"MoveOutOfWorld": () => {},
	});

	AddMock(controller, IID_UnitMotion, {
		"GetWalkSpeed": () => 1,
		"SetSpeedMultiplier": (speed) => {},
		"SetAcceleration": (accel) => {},
		"SetPassabilityClassName": (name) => {},
		"MoveToPointRange": (x, z, minRange, maxRange) => {},
		"StopMoving": () => {},
		"SetFacePointAfterMove": () => {},
		"GetFacePointAfterMove": () => true,
		"GetPassabilityClassName": () => "default"
	});

	AddMock(SYSTEM_ENTITY, IID_Pathfinder, {
		"GetClearance": () => 1,
		"GetPassabilityClass": () => 16
	});

	AddMock(controller, IID_Attack, {
		"GetRange": function() { return { "max": 10, "min": 0 }; },
		"CanAttackAsFormation": function() { return false; },
	});

	controllerAI.OnCreate();

	controllerFormation.SetMembers(units);

	controllerAI.Attack(enemy);

	for (const ent of unitAIs)
		TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.COMBAT.ATTACKING");

	controllerAI.MoveIntoFormation({ "name": "Circle" });

	// let all units be in position
	for (const ent of unitAIs)
		controllerFormation.SetFinishedEntity(ent);

	for (const ent of unitAIs)
		TS_ASSERT_EQUALS(unitAI.fsmStateName, "INDIVIDUAL.COMBAT.ATTACKING");

	controllerFormation.Disband();
}

TestFormationExiting(0);
TestFormationExiting(1);
TestFormationExiting(2);

TestMoveIntoFormationWhileAttacking();


function TestWalkAndFightTargets()
{
	const ent = 10;
	const unitAI = ConstructComponent(ent, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive",
		"FleeDistance": 10
	});
	unitAI.OnCreate();
	unitAI.losAttackRangeQuery = true;

	// The result is stored here
	let result;
	unitAI.PushOrderFront = function(type, order)
	{
		if (type === "Attack" && order?.target)
			result = order.target;
	};

	// Create some targets.
	AddMock(ent+1, IID_UnitAI, { "IsAnimal": () => true, "IsDangerousAnimal": () => false });
	AddMock(ent+2, IID_Ownership, { "GetOwner": () => 2 });
	AddMock(ent+3, IID_Ownership, { "GetOwner": () => 2 });
	AddMock(ent+4, IID_Ownership, { "GetOwner": () => 2 });
	AddMock(ent+5, IID_Ownership, { "GetOwner": () => 2 });
	AddMock(ent+6, IID_Ownership, { "GetOwner": () => 2 });
	AddMock(ent+7, IID_Ownership, { "GetOwner": () => 2 });

	unitAI.CanAttack = function(target)
	{
		return target !== ent+2 && target !== ent+7;
	};

	AddMock(ent, IID_Attack, {
		"GetPreference": (target) => ({
			[ent+4]: 0,
			[ent+5]: 1,
			[ent+6]: 2,
			[ent+7]: 0
		}?.[target])
	});

	const runTest = function(ents, res)
	{
		result = undefined;
		AddMock(SYSTEM_ENTITY, IID_RangeManager, {
			"ResetActiveQuery": () => ents
		});
		TS_ASSERT_EQUALS(unitAI.FindWalkAndFightTargets(), !!res);
		TS_ASSERT_EQUALS(result, res);
	};

	// No entities.
	runTest([]);

	// Entities that cannot be attacked.
	runTest([ent+1, ent+2, ent+7]);

	// No preference, one attackable entity.
	runTest([ent+1, ent+2, ent+3], ent+3);

	// Check preferences.
	runTest([ent+1, ent+2, ent+3, ent+4], ent+4);
	runTest([ent+1, ent+2, ent+3, ent+4, ent+5], ent+4);
	runTest([ent+1, ent+2, ent+6, ent+3, ent+4, ent+5], ent+4);
	runTest([ent+1, ent+2, ent+7, ent+6, ent+3, ent+4, ent+5], ent+4);
	runTest([ent+1, ent+2, ent+7, ent+6, ent+3, ent+5], ent+5);
	runTest([ent+1, ent+2, ent+7, ent+6, ent+3], ent+6);
	runTest([ent+1, ent+2, ent+7, ent+3], ent+3);
}

TestWalkAndFightTargets();

function TestAttemptObstructionMitigation()
{
	ResetState();

	const controllerID = 100;
	const member1ID = 201;
	const member2ID = 202;
	const member3ID = 203;
	const playerID = 1;
	const playerEntity = 5;

	// Helper function to create timer mocks
	function createTimerMock()
	{
		let timerId = null;
		let canceledTimer = null;

		return {
			"SetTimeout": function(entity, iid, functionName, time, data)
			{
				timerId = 123;
				return timerId;
			},
			"CancelTimer": function(id)
			{
				canceledTimer = id;
			},
			"SetInterval": function() { return null; }
		};
	}

	AddMock(SYSTEM_ENTITY, IID_Timer, createTimerMock(true));

	AddMock(SYSTEM_ENTITY, IID_PlayerManager, {
		"GetPlayerByID": id => playerEntity,
		"GetNumPlayers": () => 2
	});

	AddMock(SYSTEM_ENTITY, IID_Pathfinder, {
		"GetClearance": () => 1,
		"GetPassabilityClass": () => 16
	});

	AddMock(playerEntity, IID_Player, {
		"GetPlayerID": () => playerID
	});

	// Create controller UnitAI
	const controllerAI = ConstructComponent(controllerID, "UnitAI", {
		"FormationController": "true",
		"DefaultStance": "aggressive"
	});

	// Mock controller position
	let controllerX = 0;
	let controllerZ = 0;
	AddMock(controllerID, IID_Position, {
		"IsInWorld": () => true,
		"GetPosition": () => new Vector3D(controllerX, 0, controllerZ),
		"GetPosition2D": () => new Vector2D(controllerX, controllerZ),
		"GetRotation": () => ({ "y": 0 }),
		"GetTurretParent": () => INVALID_ENTITY,
		"TurnTo": () => {},
		"JumpTo": function(x, z)
		{
			controllerX = x;
			controllerZ = z;
		},
		"MoveOutOfWorld": () => {}
	});

	AddMock(controllerID, IID_Ownership, {
		"GetOwner": () => playerID,
	});

	AddMock(controllerID, IID_UnitMotion, {
		"GetWalkSpeed": () => 1,
		"GetAcceleration": () => 1,
		"StopMoving": () => {},
		"MoveToTargetRange": () => true,
		"MoveToPointRange": () => true,
		"SetSpeedMultiplier": () => {},
		"SetAcceleration": () => {},
		"SetPassabilityClassName": () => {},
		"SetFacePointAfterMove": () => {},
		"GetFacePointAfterMove": () => true,
		"GetPassabilityClassName": () => "default",
		"SetMemberOfFormation": () => {},
		"FaceTowardsPoint": () => {}
	});

	controllerAI.OnCreate();

	// Helper function to reset controller position
	function resetControllerPosition(x = 0, z = 0)
	{
		controllerX = x;
		controllerZ = z;
	}

	// Helper function to test obstruction mitigation
	function testObstructionMitigation(formationMock, orderData, expectedX, expectedZ, shouldJump)
	{
		resetControllerPosition();

		if (formationMock)
		{
			AddMock(controllerID, IID_Formation, formationMock);
		}

		controllerAI.order = { "data": orderData };

		// Clear any previous flags
		delete controllerAI.obstructionMitigationAttempted;
		delete controllerAI.obstructionMitigationTimer;

		controllerAI.AttemptObstructionMitigation();

		TS_ASSERT_EQUALS(controllerX, expectedX);
		TS_ASSERT_EQUALS(controllerZ, expectedZ);

		if (shouldJump)
		{
			TS_ASSERT(controllerAI.obstructionMitigationAttempted);
			TS_ASSERT_EQUALS(controllerAI.obstructionMitigationTimer, 123);
		}
	}

	// Should not execute if already attempted
	(function()
	{
		controllerAI.obstructionMitigationAttempted = true;
		const originalX = controllerX;
		const originalZ = controllerZ;

		controllerAI.order = { "data": { "x": 100, "z": 100 } };
		controllerAI.AttemptObstructionMitigation();

		TS_ASSERT_EQUALS(controllerX, originalX);
		TS_ASSERT_EQUALS(controllerZ, originalZ);

		delete controllerAI.obstructionMitigationAttempted;
	})();

	// Should not execute without formation component
	(function()
	{
		const originalX = controllerX;
		const originalZ = controllerZ;

		controllerAI.order = { "data": { "x": 100, "z": 100 } };
		controllerAI.AttemptObstructionMitigation();

		TS_ASSERT_EQUALS(controllerX, originalX);
		TS_ASSERT_EQUALS(controllerZ, originalZ);
	})();

	// Should not execute without valid destination
	(function()
	{
		const formationMock = { "GetClosestMemberToPosition": () => member1ID };

		// Test with missing destination
		testObstructionMitigation(formationMock, {}, 0, 0, false);

		// Test with undefined x
		testObstructionMitigation(formationMock, { "z": 100 }, 0, 0, false);

		// Test with undefined z
		testObstructionMitigation(formationMock, { "x": 100 }, 0, 0, false);
	})();

	// Should not execute if no closest member found
	(function()
	{
		const formationMock = { "GetClosestMemberToPosition": () => INVALID_ENTITY };
		testObstructionMitigation(formationMock, { "x": 100, "z": 100 }, 0, 0, false);
	})();

	// Should not execute if member or controller missing position component
	(function()
	{
		const formationMock = { "GetClosestMemberToPosition": () => member1ID };
		testObstructionMitigation(formationMock, { "x": 100, "z": 100 }, 0, 0, false);
	})();

	// Should jump when member is more than 2 meters closer to destination
	(function()
	{
		AddMock(member1ID, IID_Position, {
			"GetPosition2D": () => new Vector2D(90, 90)
		});

		const formationMock = {
			"GetClosestMemberToPosition": () => member1ID
		};

		testObstructionMitigation(formationMock, { "x": 100, "z": 100 }, 90, 90, true);
	})();

	// Should NOT jump when member is NOT more than 2 meters closer
	(function()
	{
		AddMock(member1ID, IID_Position, {
			"GetPosition2D": () => new Vector2D(95, 96)
		});

		const formationMock = {
			"GetClosestMemberToPosition": () => member1ID
		};

		controllerX = 95;
		controllerZ = 95;

		controllerAI.order = { "data": { "x": 100, "z": 100 } };
		delete controllerAI.obstructionMitigationAttempted;

		controllerAI.AttemptObstructionMitigation();

		TS_ASSERT_EQUALS(controllerX, 95);
		TS_ASSERT_EQUALS(controllerZ, 95);
		TS_ASSERT(controllerAI.obstructionMitigationAttempted);
	})();

	// Should NOT jump when member is actually farther away
	(function()
	{
		AddMock(member1ID, IID_Position, {
			"GetPosition2D": () => new Vector2D(0, 0)
		});

		const formationMock = {
			"GetClosestMemberToPosition": () => member1ID
		};

		controllerX = 95;
		controllerZ = 95;

		controllerAI.order = { "data": { "x": 100, "z": 100 } };
		delete controllerAI.obstructionMitigationAttempted;

		controllerAI.AttemptObstructionMitigation();

		TS_ASSERT_EQUALS(controllerX, 95);
		TS_ASSERT_EQUALS(controllerZ, 95);
		TS_ASSERT(controllerAI.obstructionMitigationAttempted);
	})();

	// Should jump when member is exactly 2.1 meters closer (edge case)
	(function()
	{
		AddMock(member1ID, IID_Position, {
			"GetPosition2D": () => new Vector2D(2, 1)
		});

		const formationMock = {
			"GetClosestMemberToPosition": () => member1ID
		};

		testObstructionMitigation(formationMock, { "x": 100, "z": 100 }, 2, 1, true);
	})();

	// Test SetObstructionMitigationFlag and ResetObstructionMitigationFlag
	(function()
	{
		// Use SetTimeout version for this test
		AddMock(SYSTEM_ENTITY, IID_Timer, createTimerMock(true));

		controllerAI.SetObstructionMitigationFlag();

		TS_ASSERT(controllerAI.obstructionMitigationAttempted);
		TS_ASSERT_EQUALS(controllerAI.obstructionMitigationTimer, 123);

		controllerAI.ResetObstructionMitigationFlag();

		TS_ASSERT(!controllerAI.obstructionMitigationAttempted);
	})();

	// Multiple members, should pick closest one
	(function()
	{
		const members = [member1ID, member2ID, member3ID];

		AddMock(member1ID, IID_Position, {
			"GetPosition2D": () => ({ "x": 80, "y": 80 }),
			"IsInWorld": () => true
		});

		AddMock(member2ID, IID_Position, {
			"GetPosition2D": () => ({ "x": 90, "y": 90 }),
			"IsInWorld": () => true
		});

		AddMock(member3ID, IID_Position, {
			"GetPosition2D": () => ({ "x": 50, "y": 50 }),
			"IsInWorld": () => true
		});

		const formationMock = {
			"GetClosestMemberToPosition": function(targetPosition, filter)
			{
				const memberPositions = {
					[member1ID]: { "x": 80, "y": 80 },
					[member2ID]: { "x": 90, "y": 90 },
					[member3ID]: { "x": 50, "y": 50 }
				};

				let closestMember = INVALID_ENTITY;
				let closestDistance = Infinity;

				for (const member of members)
				{
					if (filter && !filter(member))
						continue;

					const memberPos = memberPositions[member];
					if (!memberPos)
						continue;

					const dist = (targetPosition.x - memberPos.x) ** 2 + (targetPosition.y - memberPos.y) ** 2;
					if (dist < closestDistance)
					{
						closestMember = member;
						closestDistance = dist;
					}
				}
				return closestMember;
			}
		};

		testObstructionMitigation(formationMock, { "x": 100, "z": 100 }, 90, 90, true);
	})();
}

TestAttemptObstructionMitigation();
function TestFindAndSetNextTargetBehavior()
{
	ResetState();

	const attacker = 10;
	const targets = [20, 21, 22, 23, 24]; // Multiple targets for comprehensive testing

	// Basic mocks needed for unitAI creation
	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": () => {},
		"SetTimeout": () => {}
	});

	AddMock(SYSTEM_ENTITY, IID_RangeManager, {
		"CreateActiveQuery": () => 1,
		"EnableActiveQuery": () => {},
		"ResetActiveQuery": () => [],
		"DisableActiveQuery": () => {},
		"GetEntityFlagMask": () => 0
	});

	AddMock(SYSTEM_ENTITY, IID_ObstructionManager, {
		"IsInTargetRange": () => false
	});

	// Create the unitAI component
	const unitAI = ConstructComponent(attacker, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive"
	});

	AddMock(attacker, IID_Attack, {
		"GetBestAttackAgainst": () => "Melee"
	});

	unitAI.OnCreate();

	// Mock the visibility and attackability checks
	unitAI.CanAttack = (target) =>
	{
		// Target 21 is always unattackable (e.g., garrisoned, dead, etc.)
		return target !== 21;
	};

	unitAI.CheckTargetVisible = (target) =>
	{
		// Target 22 is always not visible
		return target !== 22;
	};

	// First target is valid
	(function()
	{
		const data = {
			"targetArray": [20, 21, 22, 23, 24],
			"currentTargetIndex": 0, // Currently targeting 20
			"target": 20,
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(found);
		TS_ASSERT_EQUALS(data.target, 20);
		TS_ASSERT_EQUALS(data.currentTargetIndex, 0);
	})();

	// End of array behavior
	(function()
	{
		const data = {
			"targetArray": [18, 19, 20, 21, 22],
			"currentTargetIndex": 2, // Currently targeting 22
			"allowCapture": false
		};

		// 21 and 22 are invalid, should go backward and find 20
		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(found);
		TS_ASSERT_EQUALS(data.target, 20); // Should be assigned targetArray[initialTarget - 1]
	})();

	// No valid targets found
	(function()
	{
		const data = {
			"targetArray": [21, 22], // Both are invalid (21 unattackable, 22 not visible)
			"currentTargetIndex": 0,
			"target": 21,
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(!found);
		TS_ASSERT_EQUALS(data.target, 21); // Should remain unchanged
		TS_ASSERT_EQUALS(data.currentTargetIndex, 0);
	})();

	// Empty target array
	(function()
	{
		const data = {
			"targetArray": [],
			"currentTargetIndex": 0,
			"target": 20,
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(!found);
		TS_ASSERT_EQUALS(data.target, 20); // Should remain unchanged
		TS_ASSERT_EQUALS(data.currentTargetIndex, void 0); // Should nullify currentTargetIndex
	})();

	// Current target is last in array
	(function()
	{
		const data = {
			"targetArray": [20, 21, 22, 23, 24],
			"currentTargetIndex": 4, // Currently targeting 24 (last element)
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(found);
		TS_ASSERT_EQUALS(data.target, 24); // Should remain unchanged
		TS_ASSERT_EQUALS(data.currentTargetIndex, 4);
	})();

	// Single invalid target
	(function()
	{
		const data = {
			"targetArray": [21], // Only one target, which is invalid
			"currentTargetIndex": 0,
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(!found);
	})();

	// Verify if we don't do more calls then necessary
	(function()
	{
		const searchOrder = [];

		unitAI.CanAttack = (target) =>
		{
			searchOrder.push(target);
			return target == 20;
		};

		unitAI.CheckTargetVisible = (target) => true; // All visible

		const data = {
			"targetArray": [20, 21, 22, 23],
			"currentTargetIndex": 1, // Currently targeting 21
			"allowCapture": false
		};

		const found = unitAI.FindAndSetNextTarget(data);
		TS_ASSERT(found);
		TS_ASSERT_EQUALS(searchOrder.length, 4); // Should find at 20 (4 calls)

	})();
}

TestFindAndSetNextTargetBehavior();

function TestAttackGroupBehavior()
{
	ResetState();

	const attacker1 = 100;
	const attacker2 = 101;
	const targets = [200, 201, 202, 203, 204];

	// Basic mocks needed for unitAI creation
	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": () => {},
		"SetTimeout": () => {}
	});

	AddMock(SYSTEM_ENTITY, IID_RangeManager, {
		"CreateActiveQuery": () => 1,
		"EnableActiveQuery": () => {},
		"ResetActiveQuery": () => [],
		"DisableActiveQuery": () => {},
		"GetEntityFlagMask": () => 0
	});

	AddMock(SYSTEM_ENTITY, IID_ObstructionManager, {
		"IsInTargetRange": () => false
	});

	// Create two unitAI components
	const unitAI1 = ConstructComponent(attacker1, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive"
	});

	const unitAI2 = ConstructComponent(attacker2, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive"
	});

	AddMock(attacker1, IID_Attack, {
		"GetBestAttackAgainst": () => "Melee"
	});

	AddMock(attacker2, IID_Attack, {
		"GetBestAttackAgainst": () => "Melee"
	});

	unitAI1.OnCreate();
	unitAI2.OnCreate();

	// Basic AttackGroup distribution
	(function()
	{
		let orderAdded1 = null;
		let orderAdded2 = null;

		// Mock AddOrder to capture what orders are created
		unitAI1.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded1 = { type, data, queued, pushFront };
		};

		unitAI2.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded2 = { type, data, queued, pushFront };
		};

		// Call AttackGroup for both units
		unitAI1.AttackGroup(targets, 0, 2, false, false, false);
		unitAI2.AttackGroup(targets, 1, 2, false, false, false);

		// Verify unit1's order
		TS_ASSERT(orderAdded1);
		TS_ASSERT_EQUALS(orderAdded1.type, "Attack");
		TS_ASSERT_EQUALS(orderAdded1.data.targetArray.length, 5);
		TS_ASSERT_EQUALS(orderAdded1.data.target, INVALID_ENTITY);
		// Unit 0 should start at position 1 ==> Math.round((0 + 0.5) * 2.5) = Math.round(1.25) = 1
		TS_ASSERT_EQUALS(orderAdded1.data.currentTargetIndex, 1);

		// Verify unit2's order
		TS_ASSERT(orderAdded2);
		TS_ASSERT_EQUALS(orderAdded2.type, "Attack");
		TS_ASSERT_EQUALS(orderAdded2.data.targetArray.length, 5);
		TS_ASSERT_EQUALS(orderAdded2.data.target, INVALID_ENTITY);
		// Unit 1 should start at position 4 ==> Math.round((1 + 0.5) * 2.5) = Math.round(3.75) = 4
		TS_ASSERT_EQUALS(orderAdded2.data.currentTargetIndex, 4);
	})();

	// More units than targets
	(function()
	{
		let orderAdded1 = null;
		let orderAdded2 = null;
		let orderAdded3 = null;

		const unitAI3 = ConstructComponent(102, "UnitAI", {
			"FormationController": "false",
			"DefaultStance": "aggressive"
		});
		AddMock(102, IID_Attack, { "GetBestAttackAgainst": () => "Melee" });
		unitAI3.OnCreate();

		unitAI1.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded1 = { type, data, queued, pushFront };
		};
		unitAI2.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded2 = { type, data, queued, pushFront };
		};
		unitAI3.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded3 = { type, data, queued, pushFront };
		};

		const smallTargets = [300, 301];

		unitAI1.AttackGroup(smallTargets, 0, 3, true, true, false);
		unitAI2.AttackGroup(smallTargets, 1, 3, true, true, false);
		unitAI3.AttackGroup(smallTargets, 2, 3, true, true, false);

		// Verify distribution when more units than targets
		TS_ASSERT(orderAdded1);
		TS_ASSERT_EQUALS(orderAdded1.data.currentTargetIndex, 0); // 0 * 2 / 3 = 0
		TS_ASSERT_EQUALS(orderAdded1.data.allowCapture, true);
		TS_ASSERT_EQUALS(orderAdded1.queued, true);

		TS_ASSERT(orderAdded2);
		TS_ASSERT_EQUALS(orderAdded2.data.currentTargetIndex, 1); // 1 * 2 / 3 = 0.66, round = 1
		TS_ASSERT(orderAdded2.data.currentTargetIndex < smallTargets.length);

		TS_ASSERT(orderAdded3);
		TS_ASSERT_EQUALS(orderAdded3.data.currentTargetIndex, 1); // 2 * 2 / 3 = 1.33, round = 1
		TS_ASSERT(orderAdded3.data.currentTargetIndex < smallTargets.length);
	})();

	// Edge case - single target
	(function()
	{
		let orderAdded = null;
		unitAI1.AddOrder = (type, data, queued, pushFront) =>
		{
			orderAdded = { type, data, queued, pushFront };
		};

		const singleTarget = [400];

		unitAI1.AttackGroup(singleTarget, 0, 1, false, false, true);

		TS_ASSERT(orderAdded);
		TS_ASSERT_EQUALS(orderAdded.data.targetArray.length, 1);
		TS_ASSERT_EQUALS(orderAdded.data.currentTargetIndex, 0);
		TS_ASSERT_EQUALS(orderAdded.pushFront, true);
	})();

	// Empty targets array should do nothing
	(function()
	{
		let callCount = 0;
		unitAI1.AddOrder = () => { callCount++; };

		unitAI1.AttackGroup([], 0, 1);
		TS_ASSERT_EQUALS(callCount, 0);

		unitAI1.AttackGroup(null, 0, 1);
		TS_ASSERT_EQUALS(callCount, 0);

		unitAI1.AttackGroup(undefined, 0, 1);
		TS_ASSERT_EQUALS(callCount, 0);
	})();

	// Verify RememberTargetPosition is called
	(function()
	{
		let rememberedOrder = null;
		unitAI1.RememberTargetPosition = (order) =>
		{
			rememberedOrder = order;
		};

		unitAI1.AddOrder = () => {};

		const testTargets = [500, 501];
		unitAI1.AttackGroup(testTargets, 0, 1);

		TS_ASSERT(rememberedOrder);
		TS_ASSERT_EQUALS(rememberedOrder.targetArray.length, 2);
	})();
}

function TestGatherFindingNewTargetRespectsQueuedReturnResource()
{
	ResetState();
	const ent = 10;
	const gatherTarget = 20;
	const queuedDropsite = 30;

	const resourceType = { "generic": "food", "specific": "grain" };

	AddMock(SYSTEM_ENTITY, IID_Timer, {
		"SetInterval": () => {},
		"SetTimeout": () => {}
	});
	AddMock(ent, IID_Position, {
		"IsInWorld": () => true
	});
	AddMock(ent, IID_ResourceGatherer, {
		"IsCarrying": (type) => type == resourceType.generic,
		"AddToPlayerCounter": () => {},
		"RemoveFromPlayerCounter": () => {},
		"GetLastCarriedType": () => resourceType,
		"StartGathering": () => true,
		"StopGathering": () => {}
	});

	const unitAI = ConstructComponent(ent, "UnitAI", {
		"FormationController": "false",
		"DefaultStance": "aggressive",
		"FleeDistance": 10
	});
	unitAI.OnCreate();

	unitAI.order = {
		"type": "Gather",
		"data": {
			"force": false,
			"target": gatherTarget,
			"type": resourceType,
			"template": "gaia/fruit/grain"
		}
	};
	unitAI.orderQueue = [
		unitAI.order,
		{ "type": "ReturnResource", "data": { "target": queuedDropsite, "force": false } }
	];

	let dropsiteLookups = 0;
	unitAI.FindNearestDropsite = () => { ++dropsiteLookups; return 999; };

	unitAI.CheckTargetRange = () => true;
	unitAI.UnitFsm.Init(unitAI, "INDIVIDUAL.GATHER.GATHERING");
	TS_ASSERT_EQUALS(unitAI.GetCurrentState(), "INDIVIDUAL.GATHER.GATHERING");

	// Simulates the target running out: matches ResourceGatherer.PerformGather,
	// which sends this same message on the same condition.
	unitAI.CheckTargetRange = () => false;
	unitAI.AbleToMove = () => true;
	unitAI.MoveTo = () => true;
	unitAI.ProcessMessage("TargetInvalidated", null);

	TS_ASSERT_EQUALS(dropsiteLookups, 0);
	TS_ASSERT_EQUALS(unitAI.orderQueue.length, 1);
	TS_ASSERT_EQUALS(unitAI.order.type, "ReturnResource");
	TS_ASSERT_EQUALS(unitAI.order.data.target, queuedDropsite);
	TS_ASSERT_EQUALS(unitAI.GetCurrentState(), "INDIVIDUAL.RETURNRESOURCE.APPROACHING");
}

TestGatherFindingNewTargetRespectsQueuedReturnResource();

TestAttackGroupBehavior();
