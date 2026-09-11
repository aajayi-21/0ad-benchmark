/* Copyright (C) 2025 Wildfire Games.
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

// FIFO queue of load 'functors' with time limit; enables displaying
// load progress without resorting to threads (complicated).

#include "precompiled.h"

#include "Loader.h"

#include "lib/code_annotation.h"
#include "lib/secure_crt.h"
#include "lib/utf8.h"

#include <deque>
#include <numeric>
#include <optional>
#include <string>
#include <utility>

namespace PS::Loader
{
namespace
{
// set by PS::Loader::EndRegistering; may be 0 during development when
// estimated task durations haven't yet been set.
double total_estimated_duration;

// total time spent loading so far, set by PS::Loader::ProgressiveLoad.
// we need a persistent counter so it can be reset after each load.
// this also accumulates less errors than:
// progress += task_estimated / total_estimated.
double estimated_duration_tally;

// needed for report of how long each individual task took.
double task_elapsed_time;

// main purpose is to indicate whether a load is in progress, so that
// PS::Loader::ProgressiveLoad can return 0 iff loading just completed.
// the REGISTERING state allows us to detect 2 simultaneous loads (bogus);
// FIRST_LOAD is used to skip the first timeslice (see PS::Loader::ProgressiveLoad).
enum
{
	IDLE,
	REGISTERING,
	FIRST_LOAD,
	LOADING
}
state = IDLE;


// holds all state for one load request; stored in queue.
struct LoadRequest
{
	// member documentation is in PS::Loader::Register (avoid duplication).

	LoadFunc func;

	// Translatable string shown to the player.
	std::wstring description;

	int estimated_duration_ms;

	// PS::Loader::Register gets these as parameters; pack everything together.
	LoadRequest(LoadFunc func_, std::wstring desc_, int ms_)
		: func(std::move(func_)), description(std::move(desc_)), estimated_duration_ms(ms_)
	{
	}
};

using LoadRequests = std::deque<LoadRequest>;
LoadRequests load_requests;
std::optional<PS::Loader::Task> currentTask;
} // anonymous namespace

// call before starting to register load requests.
// this routine is provided so we can prevent 2 simultaneous load operations,
// which is bogus. that can happen by clicking the load button quickly,
// or issuing via console while already loading.
void BeginRegistering()
{
	ENSURE(state == IDLE);

	state = REGISTERING;
	load_requests.clear();
}


// register a task (later processed in FIFO order).
// <func>: function that will perform the actual work; see LoadFunc.
// <param>: (optional) parameter/persistent state.
// <description>: user-visible description of the current task, e.g.
//   "Loading Textures".
// <estimated_duration_ms>: used to calculate progress, and when checking
//   whether there is enough of the time budget left to process this task
//   (reduces timeslice overruns, making the main loop more responsive).
void Register(LoadFunc func, std::wstring description, int estimatedDurationMs)
{
	ENSURE(state == REGISTERING);	// must be called between PS::Loader::(Begin|End)Register

	load_requests.push_back({std::move(func), std::move(description), estimatedDurationMs});
}


// call when finished registering tasks; subsequent calls to
// PS::Loader::ProgressiveLoad will then work off the queued entries.
void EndRegistering()
{
	ENSURE(state == REGISTERING);
	ENSURE(!load_requests.empty());

	state = FIRST_LOAD;
	estimated_duration_tally = 0.0;
	task_elapsed_time = 0.0;
	total_estimated_duration = std::accumulate(load_requests.begin(), load_requests.end(), 0.0,
	    [](double partial_result, const LoadRequest& lr) -> double { return partial_result + lr.estimated_duration_ms * 1e-3; });
}


// immediately cancel this load; no further tasks will be processed.
// used to abort loading upon user request or failure.
// note: no special notification will be returned by PS::Loader::ProgressiveLoad.
void Cancel()
{
	// the queue doesn't need to be emptied now; that'll happen during the
	// next PS::Loader::StartRegistering. for now, it is sufficient to set the
	// state, so that PS::Loader::ProgressiveLoad is a no-op.
	state = IDLE;
}

void CancelAndClear()
{
	Cancel();
	// A suspended task can own a Future borrowing state from a queued loader.
	// Destroy it first, while those owners and the game still exist.
	currentTask.reset();
	load_requests.clear();
}

namespace
{
// helper routine for PS::Loader::ProgressiveLoad.
// tries to prevent starting a long task when at the end of a timeslice.
bool HaveTimeForNextTask(double time_left, double time_budget, int estimated_duration_ms)
{
	// have already exceeded our time budget
	if(time_left <= 0.0)
		return false;

	// we haven't started a request yet this timeslice. start it even if
	// it's longer than time_budget to make sure there is progress.
	if(time_left == time_budget)
		return true;

	// check next task length. we want a lengthy task to happen in its own
	// timeslice so that its description is displayed beforehand.
	const double estimated_duration = estimated_duration_ms*1e-3;
	if(time_left+estimated_duration > time_budget*1.20)
		return false;

	return true;
}
}

ProgressiveLoadResult ProgressiveLoad(double time_budget)
{
	ProgressiveLoadResult ret;
	double progress = 0.0;	// used to set progress_percent
	double time_left = time_budget;

	// don't do any work the first time around so that a graphics update
	// happens before the first (probably lengthy) timeslice.
	if(state == FIRST_LOAD)
	{
		state = LOADING;

		ret.status = ERR::TIMED_OUT;	// make caller think we did something
		// progress already set to 0.0; that'll be passed back.
		goto done;
	}

	// we're called unconditionally from the main loop, so this isn't
	// an error; there is just nothing to do.
	if(state != LOADING)
		return {};

	while(!load_requests.empty())
	{
		// get next task; abort if there's not enough time left for it.
		const LoadRequest& lr = load_requests.front();
		const double estimated_duration = lr.estimated_duration_ms*1e-3;
		if(!HaveTimeForNextTask(time_left, time_budget, lr.estimated_duration_ms))
		{
			ret.status = ERR::TIMED_OUT;
			goto done;
		}

		// call this task's function and bill elapsed time.
		const double t0 = timer_Time();
		if (!currentTask.has_value())
			currentTask.emplace(lr.func());
		try
		{
			currentTask->Step(time_left);
		}
		catch(...)
		{
			currentTask.reset();
			throw;
		}
		const bool timed_out = !currentTask->IsDone();
		const double elapsed_time = timer_Time() - t0;
		time_left -= elapsed_time;
		task_elapsed_time += elapsed_time;

		// either finished entirely, or failed => remove from queue.
		if(!timed_out)
		{
			debug_printf("LOADER| completed %s in %g ms; estimate was %g ms\n", utf8_from_wstring(lr.description).c_str(), task_elapsed_time*1e3, estimated_duration*1e3);
			task_elapsed_time = 0.0;
			estimated_duration_tally += estimated_duration;
			load_requests.pop_front();
		}

		// calculate progress (only possible if estimates have been given)
		if(total_estimated_duration != 0.0)
		{
			double current_estimate = estimated_duration_tally;

			// function interrupted itself; add its estimated progress.
			// note: monotonicity is guaranteed since we never add more than
			//   its estimated_duration_ms.
			if(timed_out)
				current_estimate += estimated_duration * currentTask->GetProgress() / 100.0;

			progress = current_estimate / total_estimated_duration;
		}

		// do we need to continue?
		// .. function interrupted itself, i.e. timed out; abort.
		if(timed_out)
		{
			ret.status = ERR::TIMED_OUT;
			goto done;
		}

		const int taskResult{std::exchange(currentTask, std::nullopt)->Get()};
		// .. failed; abort. loading will continue when we're called in
		//    the next iteration of the main loop.
		//    rationale: bail immediately instead of remembering the first
		//    error that came up so we can report all errors that happen.
		if(taskResult < 0)
		{
			ret.status = static_cast<Status>(taskResult);
			goto done;
		}
		// .. function called PS::Loader::Cancel; abort. return OK since this is an
		//    intentional cancellation, not an error.
		if(state != LOADING)
		{
			ret.status = INFO::OK;
			goto done;
		}
		// .. succeeded; continue and process next queued task.
	}

	// queue is empty, we just finished.
	state = IDLE;
	ret.status = INFO::ALL_COMPLETE;


	// set output params (there are several return points above)
done:
	ret.progressPercent = static_cast<int>(progress * 100.0);
	ENSURE(0 <= ret.progressPercent && ret.progressPercent <= 100);

	// we want the next task, instead of what just completed:
	// it will be displayed during the next load phase.
	if(!load_requests.empty())
		ret.nextDescription = load_requests.front().description;

	debug_printf("LOADER| returning; desc=%s progress=%d\n", utf8_from_wstring(ret.nextDescription).c_str(), ret.progressPercent);

	return ret;
}


// immediately process all queued load requests.
// returns 0 on success or a negative error code.
Status NonprogressiveLoad()
{
	const double time_budget = 100.0;
		// large enough so that individual functions won't time out
		// (that'd waste time).

	for(;;)
	{
		const auto [ret, description, progress_percent] = ProgressiveLoad(time_budget);
		switch(ret)
		{
		case INFO::OK:
			debug_warn(L"No load in progress");
			return INFO::OK;
		case INFO::ALL_COMPLETE:
			return INFO::OK;
		case ERR::TIMED_OUT:
			break;			// continue loading
		default:
			WARN_RETURN_STATUS_IF_ERR(ret);	// failed; complain
		}
	}
}
} // namespace PS::Loader
