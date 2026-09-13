// Focused fixtures execute the real query/result/metric code with controlled engine inputs.
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const gui = { "timeNotificationID": 0, "timeNotifications": [] };
let territory = "neutral";
let queue = [];
const batches = new Map();
const context = {
	"SYSTEM_ENTITY": 0,
	"INVALID_ENTITY": 0,
	"clone": structuredClone,
	"BenchmarkActions": { "CheckShared"() {}, "CanApply"() { return true; } },
	"Engine": {
		"RegisterSystemComponentType"() {},
		"RegisterComponentType"() {},
		"RegisterGlobal"(name, value) { context[name] = value; },
		"GetEntitiesWithInterface"(iid) { return iid == "ProductionQueue" ? [10] : []; },
		"QueryInterface"(id, iid)
		{
			switch (iid)
			{
			case "Timer": return { "GetTime": () => 0 };
			case "GuiInterface": return gui;
			case "PlayerManager": return { "GetNumPlayers": () => 2 };
			case "Ownership": return { "GetOwner": () => 1 };
			case "ProductionQueue": return { "GetQueue": () => queue, "queue": [{ "entity": 0 }] };
			case "Trainer": return { "queue": batches };
			default: return null;
			}
		}
	},
	"ProcessCommand"()
	{
		gui.timeNotifications.push({
			"id": ++gui.timeNotificationID,
			"players": [1],
			"message": "%(name)s cannot be built in %(territoryType)s territory. Valid territories: %(validTerritories)s",
			"parameters": {
				"name": "House",
				"territoryType": { "context": "Territory type", "_string": territory },
				"validTerritories": { "context": "Territory type list", "list": ["own", "ally"] }
			}
		});
		return false;
	}
};
for (const iid of ["BenchmarkInterface", "Timer", "ProductionQueue", "GuiInterface", "PlayerManager",
	"Ownership", "ResourceGatherer", "UnitAI", "Mirage", "Foundation", "Trainer"])
	context["IID_" + iid] = iid;
for (const source of [
	"agent_benchmark/simulation/components/BenchmarkInterface.js",
	"agent_benchmark/simulation/helpers/BenchmarkObservationQueries.js",
	"public/simulation/components/Trainer.js"
])
	vm.runInNewContext(fs.readFileSync("binaries/data/mods/" + source, "utf8"), context);

const component = new context.BenchmarkInterface();
component.recording = true;
component.results = [];
for (const kind of ["neutral", "enemy", "unconnected own"])
{
	territory = kind;
	component.ExecuteAction(1, {
		"action_id": kind, "decision_id": 0, "submission_turn": 0,
		"command": { "type": "construct", "entities": [10] }
	});
	const result = component.results.at(-1);
	assert.equal(result.reason, "placement_rejected");
	assert.equal(result.message, `House cannot be built in ${kind} territory. Valid territories: own, ally`);
}

const own = Array.from({ "length": 64 }, (_, i) => ({
	"handle": "own-" + i, "position": { "x": i, "z": 1 }
}));
const view = { "observation_id": "review", "own_entities": own, "visible_entities": [], "last_seen": [],
	"map": { "bounds": { "max_x": 128, "max_z": 128 }, "cell_size": 16, "cells": [] } };
const query = { "observation_id": "review", "kind": "entities", "handles": own.map(e => e.handle) };
const entities = context.BenchmarkObservationQueries.Inspect(view, query);
assert.equal(entities.records.length, 64);
assert.equal(entities.remaining_count, 0);
const region = { "observation_id": "review", "kind": "region", "limit": 32,
	"bounds": { "min_x": 0, "min_z": 0, "max_x": 128, "max_z": 128 } };
const page1 = context.BenchmarkObservationQueries.Inspect(view, region);
const page2 = context.BenchmarkObservationQueries.Inspect(view, { ...region, "cursor": page1.next_cursor });
assert.equal(page2.records.length, 32);
assert.equal(page2.remaining_count, 0);
assert.equal(new Set([...page1.records, ...page2.records].map(e => e.handle)).size, 64);

// Actual Trainer.Progress leaves a positive fractional remainder after a blocked spawn.
const batch = new context.Trainer.prototype.Item("unit", 1, 10);
batches.set(0, batch);
batch.started = true;
batch.timeRemaining = 42;
batch.Spawn = function() { this.spawnNotified = true; };
batch.Progress(1000);
assert.equal(batch.timeRemaining, 42);
component.TelemetryEnabled = () => true;
component.intervalMetrics = new Map();
queue = [{ "timeRemaining": 42, "neededSlots": 0 }];
component.AccumulateMetrics();
assert.equal(component.intervalMetrics.get(1).blocked_producer_turns, 1);
batch.spawnNotified = false;
component.AccumulateMetrics();
queue = [{ "paused": true }];
component.AccumulateMetrics();
queue = [{ "neededSlots": 5 }];
component.AccumulateMetrics();
queue = [];
component.AccumulateMetrics();
const metrics = component.intervalMetrics.get(1);
assert.equal(metrics.active_producer_turns, 1);
assert.equal(metrics.blocked_producer_turns, 3);
assert.equal(metrics.empty_producer_turns, 1);
assert.equal(metrics.producer_turns, 5);
console.log("Placement parameters, 64-handle inspection, region paging and production transitions passed.");
