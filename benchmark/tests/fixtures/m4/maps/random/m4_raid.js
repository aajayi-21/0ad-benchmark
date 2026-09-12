Engine.LoadLibrary("rmgen");

export function* generateMap()
{
	const map = new RandomMap(3, "temp_grass");
	const place = (template, owner, x, z) => map.placeEntityAnywhere(template, owner, new Vector2D(x, z), 0);
	place("structures/athen/civil_centre", 1, 32, 32);
	place("structures/athen/storehouse", 1, 42, 30);
	place("structures/athen/house", 1, 25, 32);
	for (let i = 0; i < 8; ++i)
		place("units/athen/support_civilian", 1, 38 + i, 38);
	for (let i = 0; i < 4; ++i)
		place("units/athen/infantry_spearman_b", 1, 46 + i, 34);
	for (let i = 0; i < 6; ++i)
		place("gaia/tree/oak", 0, 44 + i, 40);
	place("structures/athen/civil_centre", 2, 96, 96);
	for (let i = 0; i < 3; ++i)
		place("units/athen/cavalry_swordsman_b", 2, 62 + i, 62);
	yield 100;
	return map;
}
