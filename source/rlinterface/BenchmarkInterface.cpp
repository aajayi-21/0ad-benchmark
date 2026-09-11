/* Copyright (C) 2026 Wildfire Games.
 * This file is part of 0 A.D.
 *
 * 0 A.D. is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 2 of the License, or
 * (at your option) any later version.
 *
 * 0 A.D. is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with 0 A.D.  If not, see <http://www.gnu.org/licenses/>.
 */

// Pull in the headers from the default precompiled header,
// even if rlinterface doesn't use precompiled headers.
#include "lib/precompiled.h"

#include "BenchmarkInterface.h"
#include "RLInterface.h"
#include "lib/build_version.h"
#include "lib/utf8.h"
#include "network/HttpServer.h"
#include "ps/CLogger.h"
#include "ps/Filesystem.h"
#include "ps/Game.h"
#include "ps/GameSetup/GameSetup.h"
#include "ps/Loader.h"
#include "ps/Replay.h"
#include "ps/scripting/JSInterface_Mod.h"
#include "scriptinterface/Interface.h"
#include "scriptinterface/JSON.h"
#include "scriptinterface/Object.h"
#include "scriptinterface/Request.h"
#include "simulation2/Simulation2.h"
#include "simulation2/components/ICmpBenchmarkInterface.h"
#include "simulation2/system/Component.h"
#include "simulation2/system/TurnManager.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <deque>
#include <fmt/format.h>
#include <httplib.h>
#include <js/PropertyAndElement.h>
#include <map>
#include <mutex>
#include <random>
#include <set>
#include <stdexcept>
#include <thread>

namespace RL
{
namespace
{
using Clock = std::chrono::steady_clock;
constexpr auto REQUEST_TIMEOUT = std::chrono::seconds(30);
constexpr size_t MAX_BODY = 1024 * 1024;
constexpr size_t MAX_CACHE_BYTES = 64 * 1024 * 1024;

struct RequestError : std::runtime_error
{
	RequestError(int status, std::string code, const std::string& message)
		: std::runtime_error(message), status(status), code(std::move(code)) {}
	int status;
	std::string code;
};

bool IsIdentifier(const std::string& text)
{
	return !text.empty() && text.size() <= 80 && std::all_of(text.begin(), text.end(), [](char c) {
		return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
			(c >= '0' && c <= '9') || c == '-' || c == '_';
	});
}

bool IsMapPath(const std::string& path)
{
	return path.size() <= 200 && path.starts_with("maps/") &&
		path.find("..") == std::string::npos && path.find("//") == std::string::npos &&
		std::all_of(path.begin(), path.end(), [](char c) {
			return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
				(c >= '0' && c <= '9') || c == '_' || c == '/' || c == '-' || c == ' ';
		});
}

std::string NewEpisodeID()
{
	std::random_device random;
	return fmt::format("{:08x}{:08x}{:08x}{:08x}", random(), random(), random(), random());
}

std::string StringField(const Script::Request& rq, JS::HandleValue object, const char* key)
{
	JS::RootedValue value(rq.cx);
	std::string result;
	if (!Script::GetProperty(rq, object, key, &value) || !value.isString() ||
		!Script::FromJSVal(rq, value, result))
		throw RequestError(400, "invalid_request", "A required string field is missing or invalid");
	return result;
}

int IntField(const Script::Request& rq, JS::HandleValue object, const char* key, int low, int high)
{
	JS::RootedValue value(rq.cx);
	if (!Script::GetProperty(rq, object, key, &value) || !value.isInt32() ||
		value.toInt32() < low || value.toInt32() > high)
		throw RequestError(400, "invalid_request", "An integer field is missing or out of range");
	return value.toInt32();
}

void Keys(const Script::Request& rq, JS::HandleValue value, std::initializer_list<const char*> allowed)
{
	if (!value.isObject())
		throw RequestError(400, "invalid_request", "Expected a JSON object");
	JS::RootedObject object(rq.cx, &value.toObject());
	bool array = false;
	if (!JS::IsArrayObject(rq.cx, object, &array) || array)
		throw RequestError(400, "invalid_request", "Expected a JSON object");
	JS::Rooted<JS::IdVector> names(rq.cx, JS::IdVector(rq.cx));
	if (!JS_Enumerate(rq.cx, object, &names))
		throw RequestError(400, "invalid_request", "Could not read object fields");
	for (size_t i = 0; i < names.length(); ++i)
	{
		JS::RootedValue name(rq.cx);
		std::string text;
		if (!JS_IdToValue(rq.cx, names[i], &name) || !Script::FromJSVal(rq, name, text) ||
			std::none_of(allowed.begin(), allowed.end(), [&](const char* key) { return text == key; }))
			throw RequestError(400, "unknown_field", "The request contains an unsupported field");
	}
}

std::vector<std::string> Names(const Script::Request& rq, JS::HandleValue object, const char* key)
{
	JS::RootedValue value(rq.cx);
	Script::GetProperty(rq, object, key, &value);
	if (value.isUndefined())
		return {};
	bool array = false;
	if (!value.isObject() || !JS::IsArrayObject(rq.cx, value, &array) || !array)
		throw RequestError(400, "invalid_request", "Catalog names must be an array");
	JS::RootedObject items(rq.cx, &value.toObject());
	uint32_t length;
	if (!JS::GetArrayLength(rq.cx, items, &length) || length > 64)
		throw RequestError(400, "invalid_request", "At most 64 catalog names are allowed");
	std::vector<std::string> result;
	for (uint32_t i = 0; i < length; ++i)
	{
		JS::RootedValue item(rq.cx);
		std::string text;
		if (!JS_GetElement(rq.cx, items, i, &item) || !item.isString() ||
			!Script::FromJSVal(rq, item, text) || text.size() > 200)
			throw RequestError(400, "invalid_request", "Invalid catalog name");
		result.push_back(std::move(text));
	}
	return result;
}
}

struct BenchmarkInterface::Impl
{
	struct Response { int status; std::string body; };
	struct Job
	{
		std::string operation, id, body;
		Clock::time_point deadline = Clock::now() + REQUEST_TIMEOUT;
		bool done = false, cancelled = false;
		Response response;
	};
	struct CachedResponse { std::string request; Response response; };

	std::unique_ptr<httplib::Server> server;
	std::thread thread;
	std::mutex mutex;
	std::condition_variable wake;
	std::deque<std::shared_ptr<Job>> queue;
	bool stopping = false;
	// The following metadata is copied for HTTP health/errors under mutex.
	std::string state = "ready", episode;
	uint32_t turn = 0;
	uint64_t timeMs = 0;
	// Main-thread-only data, including all SpiderMonkey objects and simulation access.
	Script::Interface script{"Engine", "benchmark-transport", *g_ScriptContext};
	std::vector<int> seats;
	std::string snapshot, replayDirectory, finalData;
	std::string healthData;
	std::map<std::string, CachedResponse> completed;
	size_t cacheBytes = 0;
	bool quit = false;

	Response Reply(const std::string& id, int status, const std::string& data,
		const std::string& code = {}, const std::string& message = {}, bool includeEpisode = true)
	{
		// IDs and error strings passed here contain no JSON metacharacters.
		std::lock_guard lock(mutex);
		return {status, fmt::format(
			R"({{"protocol_version":"1.0","episode_id":"{}","request_id":"{}","turn":{},"sim_time_ms":{},"state":"{}","ok":{},"data":{},"error":{}}})",
			includeEpisode ? episode : "", id, includeEpisode ? turn : 0, includeEpisode ? timeMs : 0,
			includeEpisode ? state : "unavailable", status < 400 ? "true" : "false", data,
			code.empty() ? "null" : fmt::format(R"({{"code":"{}","message":"{}"}})", code, message))};
	}

	explicit Impl(const std::string& address)
	{
		if (!VfsFileExists(L"simulation/components/BenchmarkInterface.js"))
			throw RequestError(400, "configuration", "Load the agent_benchmark mod after public");
		const char* token = std::getenv("ZERO_AD_BENCHMARK_TOKEN");
		if (!token || std::string(token).size() < 32 || std::string(token).size() > 256)
			throw RequestError(400, "configuration", "ZERO_AD_BENCHMARK_TOKEN must contain 32 to 256 characters");
		const std::string authorization = "Bearer " + std::string(token);
		const std::string prefix = "127.0.0.1:";
		if (!address.starts_with(prefix))
			throw RequestError(400, "configuration", "Benchmark interface must bind to 127.0.0.1");
		const std::string portText = address.substr(prefix.size());
		if (portText.empty() || portText.size() > 5 || !std::all_of(portText.begin(), portText.end(),
			[](char c) { return c >= '0' && c <= '9'; }))
			throw RequestError(400, "configuration", "Invalid benchmark port");
		const int port = std::stoi(portText);
		if (port < 1 || port > 65535)
			throw RequestError(400, "configuration", "Invalid benchmark port");
		{
			Script::Request rq(script);
			JS::RootedValue info(rq.cx, JSI_Mod::GetEngineInfo(script));
			// GetEngineInfo returns a frozen object; use a detached copy for transport metadata.
			const std::string engineInfo = Script::StringifyJSON(rq, &info, false);
			Script::ParseJSON(rq, engineInfo, &info);
			Script::SetProperty(rq, info, "build_version", utf8_from_wstring(build_version));
			Script::SetProperty(rq, info, "capabilities", std::vector<std::string>{
				"reset", "observe", "catalog", "advance_wait", "finalize", "shutdown"});
			Script::SetProperty(rq, info, "player_coverage", std::string("owned_only_m1"));
			healthData = Script::StringifyJSON(rq, &info, false);
		}

		server = PS::Net::createHttpServer();
		// cpp-httplib defaults to SO_REUSEPORT on Linux, which would split requests
		// between unrelated episodes. Use an exclusive listener, even during restart.
		server->set_socket_options([](socket_t) {});
		// Request handlers wait for the main thread. Do not occupy simulation worker threads.
		server->new_task_queue = [] { return new httplib::ThreadPool(4, 16); };
		server->set_payload_max_length(MAX_BODY);
		server->set_read_timeout(5, 0);
		server->set_write_timeout(5, 0);
		server->set_keep_alive_max_count(8);
		auto handler = [this, authorization](const httplib::Request& request, httplib::Response& response) {
			Response result;
			std::string id = request.get_header_value("X-Request-ID");
			if (!IsIdentifier(id))
				id.clear();
			if (request.get_header_value("Authorization") != authorization)
				result = Reply(id, 401, "null", "unauthorized", "A runner token is required", false);
			else if (request.path == "/benchmark/v1/health")
				result = Reply(id, 200, healthData);
			else if (id.empty())
				result = Reply({}, 400, "null", "invalid_request_id", "A valid X-Request-ID header is required");
			else
				result = Submit(request.path.substr(std::string("/benchmark/v1/").size()), id, request.body);
			response.status = result.status;
			response.set_content(result.body, "application/json");
		};
		server->Get("/benchmark/v1/health", handler);
		for (const char* operation : {"reset", "observe", "catalog", "advance", "finalize", "shutdown"})
			server->Post(std::string("/benchmark/v1/") + operation, handler);
		server->set_error_handler([this](const httplib::Request&, httplib::Response& response) {
			if (response.body.empty())
				response.set_content(Reply({}, response.status, "null", "http_error", "HTTP request rejected", false).body, "application/json");
		});
		if (!server->bind_to_port("127.0.0.1", port))
			throw RequestError(500, "bind_failed", "Could not bind benchmark interface port");
		thread = std::thread([this] { server->listen_after_bind(); });
		LOGMESSAGE("Benchmark interface listening on %s", address.c_str());
	}

	~Impl()
	{
		{
			std::lock_guard lock(mutex);
			stopping = true;
			for (auto& job : queue)
				job->cancelled = true;
		}
		wake.notify_all();
		server->stop();
		if (thread.joinable())
			thread.join();
	}

	Response Submit(const std::string& operation, const std::string& id, const std::string& body)
	{
		auto job = std::make_shared<Job>();
		job->operation = operation;
		job->id = id;
		job->body = body;
		std::unique_lock lock(mutex);
		if (stopping || queue.size() >= 16)
		{
			lock.unlock();
			return Reply(id, 503, "null", "busy", "Request queue is unavailable");
		}
		queue.push_back(job);
		wake.notify_all();
		wake.wait_until(lock, job->deadline, [&] { return job->done || stopping; });
		if (job->done)
			return job->response;
		job->cancelled = true;
		const bool shuttingDown = stopping;
		lock.unlock();
		if (shuttingDown)
			return Reply(id, 503, "null", "shutting_down", "The interface is shutting down");
		return Reply(id, 504, "null", "request_timeout", "Request expired; check health before continuing");
	}

	void SetState(const std::string& value)
	{
		std::lock_guard lock(mutex);
		state = value;
	}

	void CheckDeadline(const Job& job)
	{
		std::lock_guard lock(mutex);
		if (job.cancelled || Clock::now() >= job.deadline)
			throw RequestError(504, "request_timeout", "Operation exceeded its deadline");
	}

	void RefreshSnapshot()
	{
		Script::Request rq(g_Game->GetSimulation2()->GetScriptInterface());
		CmpPtr<ICmpBenchmarkInterface> component(*g_Game->GetSimulation2(), SYSTEM_ENTITY);
		if (!component)
			throw RequestError(500, "missing_mod", "The agent_benchmark mod is required");
		JS::RootedValue value(rq.cx);
		component->GetSnapshot(&value, seats);
		if (!value.isObject())
			throw RequestError(500, "snapshot_failed", "Could not build benchmark observations");
		double time = 0;
		if (!Script::GetProperty(rq, value, "sim_time_ms", time))
			throw RequestError(500, "snapshot_failed", "Missing simulation clock");
		snapshot = Script::StringifyJSON(rq, &value, false);
		if (snapshot.empty())
			throw RequestError(500, "snapshot_failed", "Could not encode benchmark observations");
		std::lock_guard lock(mutex);
		turn = g_Game->GetTurnManager()->GetCurrentTurn();
		timeMs = static_cast<uint64_t>(time);
		state = g_Game->IsGameFinished() ? "terminal" : "running";
	}

	std::string FullData()
	{
		Script::Request rq(script);
		JS::RootedValue data(rq.cx);
		Script::ParseJSON(rq, snapshot, &data);
		Script::SetProperty(rq, data, "replay_directory", replayDirectory);
		return Script::StringifyJSON(rq, &data, false);
	}

	void Reset(const Script::Request& rq, JS::HandleValue body, const Job& job)
	{
		Keys(rq, body, {"attributes", "seats", "save_replay"});
		JS::RootedValue attributes(rq.cx), settings(rq.cx), players(rq.cx), requestedSeats(rq.cx), replay(rq.cx);
		if (!Script::GetProperty(rq, body, "attributes", &attributes) || !attributes.isObject() ||
			!Script::GetProperty(rq, attributes, "settings", &settings) || !settings.isObject())
			throw RequestError(400, "invalid_attributes", "Game attributes and settings are required");
		const std::string map = StringField(rq, attributes, "map");
		const std::string type = StringField(rq, attributes, "mapType");
		const std::string directory = type == "scenario" ? "maps/scenarios/" :
			type == "skirmish" ? "maps/skirmishes/" : "maps/random/";
		if (!IsMapPath(map) || (type != "scenario" && type != "skirmish" && type != "random") ||
			!map.starts_with(directory) || map.size() == directory.size())
			throw RequestError(400, "invalid_map", "Invalid map path or type");
		const VfsPath mapFile((map + (type == "random" ? ".js" : ".xml")).c_str());
		if (!VfsFileExists(mapFile))
			throw RequestError(400, "map_not_found", "The requested map does not exist");
		bool array = false;
		Script::GetProperty(rq, settings, "PlayerData", &players);
		if (!players.isObject() || !JS::IsArrayObject(rq.cx, players, &array) || !array)
			throw RequestError(400, "invalid_attributes", "PlayerData must be an array");
		JS::RootedObject playerArray(rq.cx, &players.toObject());
		uint32_t count;
		if (!JS::GetArrayLength(rq.cx, playerArray, &count) || count < 1 || count > 8)
			throw RequestError(400, "invalid_attributes", "Between one and eight players are required");
		for (uint32_t i = 0; i < count; ++i)
		{
			JS::RootedValue player(rq.cx);
			JS_GetElement(rq.cx, playerArray, i, &player);
			if (!player.isObject() || !JS::IsArrayObject(rq.cx, player, &array) || array)
				throw RequestError(400, "invalid_attributes", "PlayerData entries must be objects");
			const std::string civ = StringField(rq, player, "Civ");
			if (!IsIdentifier(civ) || !VfsFileExists(VfsPath(("simulation/data/civs/" + civ + ".json").c_str())))
				throw RequestError(400, "invalid_attributes", "Each player needs a resolved civilization");
			const std::string ai = StringField(rq, player, "AI");
			if (ai != "" && ai != "petra")
				throw RequestError(400, "invalid_attributes", "M1 supports no AI or Petra");
			if (!ai.empty())
			{
				IntField(rq, player, "AIDiff", 0, 5);
				const std::string behavior = StringField(rq, player, "AIBehavior");
				if (behavior != "random" && behavior != "balanced" && behavior != "aggressive" && behavior != "defensive")
					throw RequestError(400, "invalid_attributes", "Invalid Petra behavior");
			}
		}
		Script::GetProperty(rq, body, "seats", &requestedSeats);
		array = false;
		if (!requestedSeats.isObject() || !JS::IsArrayObject(rq.cx, requestedSeats, &array) || !array)
			throw RequestError(400, "invalid_seats", "Seats must be an array");
		JS::RootedObject seatArray(rq.cx, &requestedSeats.toObject());
		uint32_t seatCount;
		if (!JS::GetArrayLength(rq.cx, seatArray, &seatCount) || !seatCount || seatCount > count)
			throw RequestError(400, "invalid_seats", "Invalid number of seats");
		std::set<int> uniqueSeats;
		for (uint32_t i = 0; i < seatCount; ++i)
		{
			JS::RootedValue seat(rq.cx);
			JS_GetElement(rq.cx, seatArray, i, &seat);
			if (!seat.isInt32() || seat.toInt32() < 1 || seat.toInt32() > static_cast<int>(count) ||
				!uniqueSeats.insert(seat.toInt32()).second)
				throw RequestError(400, "invalid_seats", "Seats must be distinct valid player numbers");
		}
		Script::GetProperty(rq, body, "save_replay", &replay);
		if (!replay.isBoolean())
			throw RequestError(400, "invalid_request", "save_replay must be a boolean");
		// The runner supplies resolved attributes. M1 always freezes wall-clock progression.
		IntField(rq, attributes, "gameSpeed", 1, 1);
		JS::RootedValue engineInfo(rq.cx, JSI_Mod::GetEngineInfo(script)), mods(rq.cx);
		Script::GetProperty(rq, engineInfo, "mods", &mods);
		Script::SetProperty(rq, attributes, "mods", mods);
		if (type == "random")
			Script::SetProperty(rq, attributes, "script", map.substr(std::string("maps/random/").size()) + ".js");
		const std::string config = Script::StringifyJSON(rq, &attributes, false);
		EndGame();
		seats.assign(uniqueSeats.begin(), uniqueSeats.end());
		snapshot.clear();
		finalData.clear();
		replayDirectory.clear();
		completed.clear();
		cacheBytes = 0;
		{
			std::lock_guard lock(mutex);
			episode = NewEpisodeID();
			turn = 0;
			timeMs = 0;
			state = "loading";
		}
		try
		{
			const int errorsBefore = g_Logger->GetNumberOfErrors();
			g_Game = new CGame(replay.toBoolean());
			g_Game->SetPlayerID(seats.front());
			Script::Request simulationRequest(g_Game->GetSimulation2()->GetScriptInterface());
			JS::RootedValue gameAttributes(simulationRequest.cx);
			if (!Script::ParseJSON(simulationRequest, config, &gameAttributes))
				throw RequestError(400, "invalid_attributes", "Could not load game attributes");
			g_Game->StartGame(&gameAttributes, "");
			for (;;)
			{
				CheckDeadline(job);
				const auto progress = PS::Loader::ProgressiveLoad(0.01);
				if (progress.status == INFO::ALL_COMPLETE)
					break;
				if (progress.status != ERR::TIMED_OUT)
					throw RequestError(500, "load_failed", "The map could not be loaded");
			}
			// Some engine script loaders log an error and return normally.
			if (g_Logger->GetNumberOfErrors() != errorsBefore)
				throw RequestError(500, "load_failed", "The engine reported errors while loading");
			if (g_Game->ReallyStartGame() != PSRETURN_OK)
				throw RequestError(500, "load_failed", "The game could not be started");
			replayDirectory = g_Game->GetReplayLogger().GetDirectory().string8();
			RefreshSnapshot();
			if (g_Logger->GetNumberOfErrors() != errorsBefore)
				throw RequestError(500, "load_failed", "The engine reported errors while initializing observations");
		}
		catch (...)
		{
			PS::Loader::CancelAndClear();
			EndGame();
			SetState("failed");
			throw;
		}
	}

	Response Execute(Job& job)
	{
		Script::Request rq(script);
		JS::RootedValue body(rq.cx);
		if (!Script::ParseJSON(rq, job.body, &body) || !body.isObject())
			throw RequestError(400, "invalid_json", "Expected a JSON object body");
		if (job.operation == "shutdown")
		{
			Keys(rq, body, {});
			EndGame();
			SetState("finalized");
			quit = true;
			return Reply(job.id, 200, "{}");
		}
		if (state == "failed")
			throw RequestError(503, "process_failed", "Restart this process after a failed load or advance");
		const std::string cacheKey = job.operation + ":" + job.id;
		if (auto found = completed.find(cacheKey); found != completed.end())
		{
			if (found->second.request != job.body)
				throw RequestError(409, "request_id_conflict", "Request ID was reused with different content");
			return found->second.response;
		}
		// Finalize is idempotent by lifecycle state and remains available at the cache limit.
		const bool mutation = job.operation == "reset" || job.operation == "advance";
		if (job.operation == "advance" && (completed.size() >= 256 || cacheBytes >= MAX_CACHE_BYTES))
			throw RequestError(429, "request_limit", "Reset before exceeding the M1 mutation request limit");
		std::string result;
		if (job.operation == "reset")
		{
			Reset(rq, body, job);
			result = FullData();
		}
		else
		{
			if (episode.empty() || StringField(rq, body, "episode_id") != episode)
				throw RequestError(409, "stale_episode", "The request does not match the current episode");
			if (job.operation == "finalize")
			{
				Keys(rq, body, {"episode_id"});
				if (state != "finalized")
				{
					finalData = FullData();
					EndGame();
					SetState("finalized");
				}
				result = finalData;
			}
			else
			{
				if (state != "running" && state != "terminal")
					throw RequestError(409, "invalid_state", "This operation requires a loaded episode");
				if (job.operation == "observe")
				{
					Keys(rq, body, {"episode_id", "audience", "seat"});
					JS::RootedValue cached(rq.cx), selection(rq.cx);
					Script::ParseJSON(rq, snapshot, &cached);
					const std::string audience = StringField(rq, body, "audience");
					if (audience == "evaluator")
						Script::GetProperty(rq, cached, "evaluator", &selection);
					else if (audience == "player")
					{
						const int seat = BoundSeat(rq, body);
						JS::RootedValue views(rq.cx);
						Script::GetProperty(rq, cached, "players", &views);
						Script::GetPropertyInt(rq, views, seat, &selection);
					}
					else
						throw RequestError(400, "invalid_audience", "Audience must be player or evaluator");
					result = Script::StringifyJSON(rq, &selection, false);
				}
				else if (job.operation == "catalog")
				{
					Keys(rq, body, {"episode_id", "seat", "templates", "technologies"});
					const int seat = BoundSeat(rq, body);
					const auto names = Names(rq, body, "templates");
					const auto technologies = Names(rq, body, "technologies");
					const int errorsBefore = g_Logger->GetNumberOfErrors();
					Script::Request simulationRequest(g_Game->GetSimulation2()->GetScriptInterface());
					JS::RootedValue catalog(simulationRequest.cx);
					CmpPtr<ICmpBenchmarkInterface> component(*g_Game->GetSimulation2(), SYSTEM_ENTITY);
					component->GetCatalog(&catalog, seat, names, technologies);
					if (!catalog.isObject() || g_Logger->GetNumberOfErrors() != errorsBefore)
						throw RequestError(500, "catalog_failed", "Could not build the catalog");
					result = Script::StringifyJSON(simulationRequest, &catalog, false);
				}
				else if (job.operation == "advance")
				{
					// M1 only permits waiting so observation noninterference can be verified with Petra.
					Keys(rq, body, {"episode_id", "expected_turn", "turns"});
					if (state == "terminal")
						throw RequestError(409, "terminal", "The episode has ended");
					if (IntField(rq, body, "expected_turn", 0, 1000000) != static_cast<int>(turn))
						throw RequestError(409, "stale_turn", "The expected turn does not match");
					const int count = IntField(rq, body, "turns", 1, 300);
					try
					{
						const int errorsBefore = g_Logger->GetNumberOfErrors();
						for (int i = 0; i < count && !g_Game->IsGameFinished(); ++i)
						{
							CheckDeadline(job);
							auto* manager = g_Game->GetTurnManager();
							const auto before = manager->GetCurrentTurn();
							manager->Update(DEFAULT_TURN_LENGTH / 1000.f, 1,
								[](const std::string&, const std::optional<JS::HandleValueArray>) {});
							if (manager->GetCurrentTurn() != before + 1)
								throw RequestError(500, "advance_failed", "The engine did not complete one turn");
							if (g_Logger->GetNumberOfErrors() != errorsBefore)
								throw RequestError(500, "advance_failed", "The engine reported errors during advance");
						}
						RefreshSnapshot();
						if (g_Logger->GetNumberOfErrors() != errorsBefore)
							throw RequestError(500, "snapshot_failed", "The engine reported errors while building observations");
					}
					catch (...)
					{
						SetState("failed");
						throw;
					}
					result = FullData();
				}
			}
		}
		auto response = Reply(job.id, 200, result);
		if (mutation)
		{
			completed[cacheKey] = {job.body, response};
			cacheBytes += job.body.size() + response.body.size();
		}
		return response;
	}

	int BoundSeat(const Script::Request& rq, JS::HandleValue body)
	{
		const int seat = IntField(rq, body, "seat", 1, 8);
		if (std::find(seats.begin(), seats.end(), seat) == seats.end())
			throw RequestError(403, "unbound_seat", "This seat is not bound to the episode");
		return seat;
	}

	void Poll()
	{
		std::shared_ptr<Job> job;
		{
			std::unique_lock lock(mutex);
			wake.wait_for(lock, std::chrono::milliseconds(10), [&] { return !queue.empty() || stopping; });
			if (queue.empty())
				return;
			job = queue.front();
			queue.pop_front();
			if (job->cancelled)
				return;
		}
		Response response;
		try
		{
			CheckDeadline(*job);
			response = Execute(*job);
		}
		catch (const RequestError& error)
		{
			response = Reply(job->id, error.status, "null", error.code, error.what());
		}
		catch (const std::exception& error)
		{
			LOGERROR("Benchmark operation failed: %s", error.what());
			SetState("failed");
			response = Reply(job->id, 500, "null", "internal_error", "Operation failed; restart the process");
		}
		{
			std::lock_guard lock(mutex);
			job->response = std::move(response);
			job->done = true;
		}
		wake.notify_all();
	}
};

BenchmarkInterface::BenchmarkInterface(const std::string& address)
{
	try
	{
		m_Impl = std::make_unique<Impl>(address);
	}
	catch (const std::exception& error)
	{
		LOGERROR("Benchmark startup failed: %s", error.what());
		throw SetupError{};
	}
}

BenchmarkInterface::~BenchmarkInterface() = default;
void BenchmarkInterface::Poll() { m_Impl->Poll(); }
bool BenchmarkInterface::ShouldQuit() const { return m_Impl->quit; }
}
