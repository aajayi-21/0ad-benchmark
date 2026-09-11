// Test-only instrumentation shared by both paired runs. Preserve ordinary dispatch.
for (const type of Object.keys(g_Commands))
{
	const dispatch = g_Commands[type];
	g_Commands[type] = function(player, command, data)
	{
		print("M1_COMMAND " + JSON.stringify({
			"sim_time_ms": Engine.QueryInterface(SYSTEM_ENTITY, IID_Timer).GetTime(),
			"player": player,
			"command": command
		}) + "\n");
		return dispatch(player, command, data);
	};
}
