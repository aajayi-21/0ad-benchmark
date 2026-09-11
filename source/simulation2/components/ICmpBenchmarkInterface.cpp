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

#include "precompiled.h"

#include "ICmpBenchmarkInterface.h"
#include "simulation2/scripting/ScriptComponent.h"
#include "simulation2/system/InterfaceScripted.h"

BEGIN_INTERFACE_WRAPPER(BenchmarkInterface)
END_INTERFACE_WRAPPER(BenchmarkInterface)

class CCmpBenchmarkInterfaceScripted : public ICmpBenchmarkInterface
{
public:
	DEFAULT_SCRIPT_WRAPPER(BenchmarkInterfaceScripted)

	void UpdateKnowledge(JS::MutableHandleValue ret, const std::vector<int>& seats, uint32_t turn) override
	{
		m_Script.CallRef("UpdateKnowledge", ret, seats, turn);
	}

	void FreezeObservation(JS::MutableHandleValue ret, const std::vector<int>& seats, const std::string& episode) override
	{
		m_Script.CallRef("FreezeObservation", ret, seats, episode);
	}

	void Inspect(JS::MutableHandleValue ret, int seat, const std::string& query) override
	{
		m_Script.CallRef("Inspect", ret, seat, query);
	}

	void GetSnapshot(JS::MutableHandleValue ret, const std::vector<int>& seats) override
	{
		m_Script.CallRef("GetSnapshot", ret, seats);
	}

	void GetCatalog(JS::MutableHandleValue ret, int seat,
		const std::vector<std::string>& names, const std::vector<std::string>& technologies) override
	{
		m_Script.CallRef("GetCatalog", ret, seat, names, technologies);
	}

	void GetStatus(JS::MutableHandleValue ret, const std::vector<int>& seats) override
	{
		m_Script.CallRef("GetStatus", ret, seats);
	}

	void PrepareDecision(JS::MutableHandleValue ret, const std::string& batches,
		const std::vector<int>& seats, uint32_t turn, uint32_t decision) override
	{
		m_Script.CallRef("PrepareDecision", ret, batches, seats, turn, decision);
	}
};

REGISTER_COMPONENT_SCRIPT_WRAPPER(BenchmarkInterfaceScripted)
