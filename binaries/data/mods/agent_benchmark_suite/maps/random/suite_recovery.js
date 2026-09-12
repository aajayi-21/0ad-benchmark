Engine.LoadLibrary("rmgen");
Engine.LoadLibrary("suite");

// Recovery: a cavalry raid staged 24 tiles away will hit the workers; the agent must rebuild.
export function* generateMap()
{
	const map = suiteMap();
	const base = suiteBase(map, 1, "athen", suiteBasePosition(), 8, 4, 0, [["storehouse", 10, -2]]);
	suiteResources(map, base, 3, 2);
	const corner = suiteOppositeCorner(base);
	map.placeEntityAnywhere("structures/athen/civil_centre", 2, corner, 0);
	const staging = suiteClamp(Vector2D.add(base, Vector2D.sub(corner, base).normalize().mult(24)));
	suiteUnits(map, "units/athen/cavalry_swordsman_b", 2, staging, 3);
	yield 100;
	return map;
}
