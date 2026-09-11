// A finite fallback bound protects the test even if engine cancellation regresses.
export function* generateMap()
{
	const deadline = Engine.GetMicroseconds() + 35000000;
	while (Engine.GetMicroseconds() < deadline)
		yield 1;
	throw new Error("M1 slow map reached its fallback deadline");
}
