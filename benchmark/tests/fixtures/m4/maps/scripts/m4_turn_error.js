// A logged script error during turn 10 must retire the engine and fail the episode explicitly.
Trigger.prototype.M4TurnError = function()
{
	error("M4 fixture: deliberate simulation error");
};
Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(2000, "M4TurnError", {});
