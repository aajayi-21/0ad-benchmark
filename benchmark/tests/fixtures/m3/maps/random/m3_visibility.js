Engine.LoadLibrary("rmgen");

export function* generateMap()
{
	const map = new RandomMap(3, "temp_grass");
	const place = (template, owner, x, z) => map.placeEntityAnywhere(template, owner, new Vector2D(x, z), 0);
	place("structures/athen/civil_centre", 1, 32, 32);
	place("structures/athen/house", 1, 25, 32);
	for (let i = 0; i < 6; ++i)
		place("units/athen/support_civilian", 1, 38 + i, 38);
	place("units/athen/cavalry_swordsman_b", 1, 50, 50);
	place("structures/athen/civil_centre", 2, 96, 96);
	place("structures/athen/house", 2, 64, 50);
	place("units/athen/infantry_spearman_b", 2, 62, 50);
	place("units/athen/infantry_javelineer_a", 2, 105, 105);
	place("gaia/tree/oak", 0, 62, 52);
	yield 100;
	return map;
}
