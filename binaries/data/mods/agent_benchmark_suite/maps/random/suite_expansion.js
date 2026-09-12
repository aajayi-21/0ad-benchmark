Engine.LoadLibrary("rmgen");
Engine.LoadLibrary("suite");

// Expansion: the base sits in the west third; the fixed target region is the east-centre square
// x in [352, 448), z in [208, 304) world units (tiles 88-112 by 52-76), outside own territory.
export function* generateMap()
{
	const map = suiteMap();
	const base = suiteBase(map, 1, "athen", new Vector2D(randIntInclusive(24, 40), randIntInclusive(44, 84)), 6, 0, 1);
	suiteResources(map, base, 3, 2);
	// Wood near the region, but clear of its centre where an expansion structure fits.
	suiteCluster(map, "gaia/tree/oak", new Vector2D(92, 72), 6, 3);
	map.placeEntityAnywhere("structures/athen/civil_centre", 2, new Vector2D(114, base.y < 64 ? 114 : 14), 0);
	yield 100;
	return map;
}
