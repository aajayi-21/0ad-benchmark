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

#ifndef INCLUDED_LOADER
#define INCLUDED_LOADER

#include "lib/debug.h"
#include "lib/status.h"
#include "lib/timer.h"

#include <coroutine>
#include <exception>
#include <functional>
#include <string>
#include <utility>

namespace PS::Loader
{
/*

[KEEP IN SYNC WITH WIKI!]

Overview
--------

"Loading" is the act of preparing a game session, including reading all
required data from disk. Ideally, this would be very quick, but for complex
maps and/or low-end machines, a duration of several seconds can be expected.
Having the game freeze that long is unacceptable; instead, we want to display
the current progress and task, which greatly increases user patience.


Allowing for Display
--------------------

To display progress, we need to periodically 'interrupt' loading.
Threads come to mind, but there is a problem: since OpenGL graphics calls
must come from the main thread, loading would have to happen in a
background thread. Everything would thus need to be made thread-safe,
which is a considerable complication.

Therefore, we load from a single thread, and split the operation up into
"tasks" (as short as possible). These are typically function calls instead of
being called directly, they are registered with our queue. We are called from
the main loop and process as many tasks as possible within one "timeslice".

After that, progress is updated: an estimated duration for each task
(derived from timings on one machine) is used to calculate headway.
As long as task lengths only differ by a constant factor between machines,
this timing is exact; even if not, only smoothness of update suffers.


Interrupting Lengthy Tasks
--------------------------

The above is sufficient for basic needs, but breaks down if tasks are long
(> 500ms). To fix this, we will need to modify the tasks themselves:
either make them coroutines, i.e. have them return to the main loop and then
resume where they left off, or re-enter a limited version of the main loop.
The former requires persistent state and careful implementation,
but yields significant advantages:
- progress calculation is easy and smooth,
- all services of the main loop (especially input*) are available, and
- complexity due to reentering the main loop is avoided.

* input is important, since we want to be able to abort long loads or
even exit the game immediately.

We therefore go with the 'coroutine' (more correctly 'generator') approach.
Examples of tasks that take so long and typical implementations may
be seen in MapReader.cpp.


Intended Use
------------

  PS::Loader::BeginRegistering();
  PS::Loader::Register(..) for each sub-function
  PS::Loader::EndRegistering();
Then in the main loop, call PS::Loader::ProgressiveLoad().

*/


// NOTE: this module is not thread-safe!


// call before starting to register tasks.
// this routine is provided so we can prevent 2 simultaneous load operations,
// which is bogus. that can happen by clicking the load button quickly,
// or issuing via console while already loading.
void BeginRegistering();

/**
 * Coroutine which performs the actual work.
 *
 * `co_yield ...` can be used to yield the current progress. Iff the timeout is
 *	reached, the coroutine suspends.
 * `co_await std::suspend_always{}` is usefull to force a suspention. e.g. When
 *	no progress can be made such as when the work is done on a different
 *	thread.
 * `co_return 0` notifies the loader that the task is fineshed without an
 *	error.
 * `co_return ...` when the returned value is negative the loader interprets
 *	that as a task failure. `PS::Loader::ProgressiveLoad` will abort
 *	immediately and forward the error code.
 */
class Task
{
public:
	class promise_type
	{
		class SuspendIf
		{
		public:
			explicit SuspendIf(const bool suspend) :
				m_Suspend{suspend}
			{}

			bool await_ready() const noexcept
			{
				return !m_Suspend;
			}
			void await_suspend(std::coroutine_handle<promise_type>) const noexcept
			{}
			void await_resume() const noexcept
			{}

		private:
			bool m_Suspend;
		};
	public:
		Task get_return_object() noexcept
		{
			return Task{std::coroutine_handle<promise_type>::from_promise(*this)};
		}
		std::suspend_always initial_suspend() const noexcept { return {}; }
		std::suspend_always final_suspend() const noexcept { return {}; }
		void return_value(const int result) noexcept
		{
			m_Result = result;
		}
		void unhandled_exception() noexcept
		{
			m_Exception = std::current_exception();
		}

		SuspendIf yield_value(const int progress) noexcept
		{
			m_Progress = progress;
			return SuspendIf{m_StepEnd < timer_Time()};
		}

		int m_Progress{0};
		double m_StepEnd;
		int m_Result{0};
		std::exception_ptr m_Exception;
	};

	Task(const Task&) = delete;
	Task& operator =(const Task&) = delete;
	Task(Task&& other) noexcept :
		m_Handle{std::exchange(other.m_Handle, {})}
	{}
	Task& operator =(Task&& other) noexcept
	{
		m_Handle = std::exchange(other.m_Handle, {});
		return *this;
	}

	~Task()
	{
		if (m_Handle)
			m_Handle.destroy();
	}

	[[nodiscard]] double GetProgress() const noexcept
	{
		return m_Handle.promise().m_Progress;
	}

	void Step(const double timeBudget)
	{
		m_Handle.promise().m_StepEnd = timeBudget + timer_Time();
		m_Handle.resume();
		std::exception_ptr exception{std::exchange(m_Handle.promise().m_Exception, {})};
		if (exception)
			std::rethrow_exception(std::move(exception));
	}

	[[nodiscard]] bool IsDone() const noexcept
	{
		return m_Handle.done();
	}

	[[nodiscard]] int Get() const noexcept
	{
		return m_Handle.promise().m_Result;
	}

private:
	explicit Task(std::coroutine_handle<promise_type> h) noexcept :
		m_Handle{std::move(h)}
	{}

	std::coroutine_handle<promise_type> m_Handle;
};

using LoadFunc = std::function<PS::Loader::Task()>;

// register a task (later processed in FIFO order).
// <func>: function that will perform the actual work; see LoadFunc.
// <description>: user-visible description of the current task, e.g.
//   "Loading Textures".
// <estimated_duration_ms>: used to calculate progress, and when checking
//   whether there is enough of the time budget left to process this task
//   (reduces timeslice overruns, making the main loop more responsive).
void Register(LoadFunc func, std::wstring description, int estimated_duration_ms);


// call when finished registering tasks; subsequent calls to
// PS::Loader::ProgressiveLoad will then work off the queued entries.
void EndRegistering();


// immediately cancel this load; no further tasks will be processed.
// used to abort loading upon user request or failure.
// note: no special notification will be returned by PS::Loader::ProgressiveLoad.
void Cancel();

/**
 * Cancel loading and release suspended tasks before their game is destroyed.
 * Call on the main thread, only after ProgressiveLoad has returned or unwound;
 * never from inside a loader task. Cancelling an asynchronous task may wait for it.
 */
void CancelAndClear();

struct ProgressiveLoadResult
{
	/**
	 * @c INFO::All_COMPLETE if the final load task just completed.
	 * @c ERR::TIMED_OUT if loading is in progress but didn't finish.
	 * @c 0 if not currently loading (no-op).
	 * Otherwise an error code. the request has been de-queued.
	 */
	Status status{0};

	/**
	 * An empty string when finished.
	 * Otherwise the description of the next task that will be undertaken.
	 */
	std::wstring nextDescription;

	/**
	 * The current progress value.
	 */
	int progressPercent;
};
/**
 * Process as many of the queued tasks as possible within @c timeBudget [s].
 * if a task is lengthy, the budget may be exceeded. call from the main loop.
 */
ProgressiveLoadResult ProgressiveLoad(double time_budget);

// immediately process all queued load requests.
// returns 0 on success or a negative error code.
Status NonprogressiveLoad();

} // namespace PS::Loader

#endif	// #ifndef INCLUDED_LOADER
