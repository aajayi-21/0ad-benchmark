Engine.LoadLibrary("rmgen");
Engine.LoadLibrary("suite");

// Raid defense: the defender has a house and storehouse, eight civilians, six spearmen and four
// cavalry. The soldiers drill 40 tiles from the centre on the side away from the raiders, beyond
// their own vision of it, so a passive player never engages; the raiding player (Rome) stages a
// ram with an escort 44 tiles away, outside starting vision, so the trigger script decides when
// each wave attacks.
export function* generateMap()
{
	const map = suiteMap();
	const base = suiteBase(map, 1, "athen", suiteBasePosition(), 8, 0, 0, [["house", -8, 0], ["storehouse", 10, -2]]);
	suiteResources(map, base, 3, 2);
	const corner = suiteOppositeCorner(base);
	map.placeEntityAnywhere("structures/rome/civil_centre", 2, corner, 0);
	const direction = Vector2D.sub(corner, base).normalize();
	const drill = suiteClamp(Vector2D.sub(base, direction.mult(40)));
	suiteUnits(map, "units/athen/infantry_spearman_b", 1, new Vector2D(drill.x - 3, drill.y), 6);
	suiteUnits(map, "units/athen/cavalry_swordsman_b", 1, new Vector2D(drill.x - 2, drill.y + 3), 4);
	const staging = suiteClamp(Vector2D.add(base, direction.mult(44)));
	map.placeEntityAnywhere("units/rome/siege_ram", 2, staging, 0);
	suiteUnits(map, "units/rome/infantry_swordsman_b", 2, new Vector2D(staging.x - 2, staging.y + 2), 3);
	yield 100;
	return map;
}
