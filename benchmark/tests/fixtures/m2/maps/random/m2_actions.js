Engine.LoadLibrary("rmgen");

export function* generateMap()
{
	const map = new RandomMap(3, "temp_grass");
	const place = (template, owner, x, z) => map.placeEntityAnywhere(template, owner, new Vector2D(x, z), 0);
	place("structures/athen/civil_centre", 1, 32, 32);
	place("structures/athen/storehouse", 1, 42, 30);
	place("structures/athen/house", 1, 25, 32);
	place("structures/athen/civil_centre", 2, 82, 32);
	place("structures/athen/storehouse", 2, 63, 32);
	place("gaia/tree/oak", 0, 43, 35);
	for (let i = 0; i < 8; ++i)
		place("units/athen/support_civilian", 1, 38 + i, 38);
	for (let i = 0; i < 4; ++i)
		place("units/athen/infantry_spearman_b", 1, 48 + i, 32);
	place("units/athen/infantry_spearman_b", 2, 96, 98);
	yield 100;
	return map;
}
