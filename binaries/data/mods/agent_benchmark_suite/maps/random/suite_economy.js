Engine.LoadLibrary("rmgen");
Engine.LoadLibrary("suite");

// Economy and phase advancement: one base, generous nearby resources, a passive far opponent.
export function* generateMap()
{
	const map = suiteMap();
	const base = suiteBase(map, 1, "athen", suiteBasePosition(), 6, 0, 1);
	suiteResources(map, base, 4, 3);
	map.placeEntityAnywhere("structures/athen/civil_centre", 2, suiteOppositeCorner(base), 0);
	yield 100;
	return map;
}
