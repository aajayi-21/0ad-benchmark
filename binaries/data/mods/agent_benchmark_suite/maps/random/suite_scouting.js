Engine.LoadLibrary("rmgen");
Engine.LoadLibrary("suite");

// Scouting and response: an enemy camp of three spearmen waits 30 tiles north-east or south-west
// of the base depending on the seed's parity; it is outside starting vision and must be found.
// Units, unlike structures, cannot decay away on their own.
export function* generateMap()
{
	const map = suiteMap();
	const base = suiteBase(map, 1, "athen", suiteBasePosition(52, 76), 4, 6, 1);
	suiteResources(map, base, 2, 2);
	const sign = g_MapSettings.Seed % 2 == 0 ? 1 : -1;
	const target = suiteClamp(new Vector2D(base.x + 30 * sign, base.y + 30 * sign), 8);
	suiteUnits(map, "units/athen/infantry_spearman_b", 2, target, 3);
	map.placeEntityAnywhere("structures/athen/civil_centre", 2, suiteOppositeCorner(base), 0);
	yield 100;
	return map;
}
