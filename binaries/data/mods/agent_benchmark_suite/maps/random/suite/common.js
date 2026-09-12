/**
 * Shared placement helpers for the benchmark suite maps. Positions are tile coordinates on a
 * 128-tile map (512 world units). Every random choice uses the map seed through the ordinary
 * rmgen random functions, so a seed fully determines the layout.
 */

function suiteMap()
{
	return new RandomMap(3, "temp_grass");
}

/** Return a seeded base position inside [min, max] on both axes. */
function suiteBasePosition(min = 44, max = 84)
{
	return new Vector2D(randIntInclusive(min, max), randIntInclusive(min, max));
}

/** The corner farthest from `base`, inset so structures stay inside the map. */
function suiteOppositeCorner(base, inset = 14)
{
	const size = g_MapSettings.Size;
	return new Vector2D(base.x < size / 2 ? size - inset : inset, base.y < size / 2 ? size - inset : inset);
}

function suiteClamp(position, margin = 6)
{
	const size = g_MapSettings.Size;
	return new Vector2D(Math.min(size - margin, Math.max(margin, position.x)),
		Math.min(size - margin, Math.max(margin, position.y)));
}

/** Place `count` copies of a template around `center` within `radius` tiles. */
function suiteCluster(map, template, center, count, radius, owner = 0)
{
	for (let i = 0; i < count; ++i)
	{
		const angle = randFloat(0, 2 * Math.PI);
		const distance = randFloat(0, radius);
		const position = suiteClamp(Vector2D.add(center, new Vector2D(Math.cos(angle), Math.sin(angle)).mult(distance)));
		map.placeEntityAnywhere(template, owner, position, randomAngle());
	}
}

/** Place a row of units near `origin`, offset along x. */
function suiteUnits(map, template, owner, origin, count, spacing = 1.2)
{
	for (let i = 0; i < count; ++i)
		map.placeEntityAnywhere(template, owner, suiteClamp(new Vector2D(origin.x + i * spacing, origin.y)), 0);
}

/** Resource clusters at seeded angles: `wood` oak clusters and `food` berry groves. */
function suiteResources(map, base, wood, food)
{
	const start = randFloat(0, 2 * Math.PI);
	for (let i = 0; i < wood; ++i)
	{
		const angle = start + i * 2 * Math.PI / wood + randFloat(-0.3, 0.3);
		const center = suiteClamp(Vector2D.add(base, new Vector2D(Math.cos(angle), Math.sin(angle)).mult(randFloat(13, 18))));
		suiteCluster(map, "gaia/tree/oak", center, 8, 3);
	}
	for (let i = 0; i < food; ++i)
	{
		const angle = start + Math.PI / wood + i * 2 * Math.PI / food + randFloat(-0.3, 0.3);
		const center = suiteClamp(Vector2D.add(base, new Vector2D(Math.cos(angle), Math.sin(angle)).mult(randFloat(9, 12))));
		suiteCluster(map, "gaia/fruit/berry_01", center, 5, 2);
	}
}

/** A civic centre plus a civilian row and optional soldiers; returns the base position. */
function suiteBase(map, owner, civ, base, civilians, soldiers = 0, cavalry = 0, extras = [])
{
	map.placeEntityAnywhere("structures/" + civ + "/civil_centre", owner, base, 0);
	suiteUnits(map, "units/" + civ + "/support_civilian", owner, new Vector2D(base.x + 6, base.y + 6), civilians);
	suiteUnits(map, "units/" + civ + "/infantry_spearman_b", owner, new Vector2D(base.x + 6, base.y + 4), soldiers);
	suiteUnits(map, "units/" + civ + "/cavalry_swordsman_b", owner, new Vector2D(base.x + 4, base.y + 8), cavalry);
	for (const [template, dx, dz] of extras)
		map.placeEntityAnywhere("structures/" + civ + "/" + template, owner, suiteClamp(new Vector2D(base.x + dx, base.y + dz)), 0);
	return base;
}
