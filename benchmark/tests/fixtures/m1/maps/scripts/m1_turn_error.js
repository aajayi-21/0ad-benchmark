Trigger.prototype.M1TurnError = function()
{
	throw new Error("Intentional M1 turn failure");
};
Engine.QueryInterface(SYSTEM_ENTITY, IID_Trigger).DoAfterDelay(0, "M1TurnError", {});
