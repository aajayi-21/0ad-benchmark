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

#ifndef INCLUDED_ICMPBENCHMARKINTERFACE
#define INCLUDED_ICMPBENCHMARKINTERFACE

#include "simulation2/system/Component.h"
#include "simulation2/system/Interface.h"

#include <js/TypeDecls.h>
#include <string>
#include <vector>

/** Main-thread bridge to the optional agent_benchmark simulation mod. */
class ICmpBenchmarkInterface : public IComponent
{
public:
	virtual void GetSnapshot(JS::MutableHandleValue ret, const std::vector<int>& seats) = 0;
	virtual void GetCatalog(JS::MutableHandleValue ret, int seat,
		const std::vector<std::string>& names, const std::vector<std::string>& technologies) = 0;

	DECLARE_INTERFACE_TYPE(BenchmarkInterface)
};

#endif // INCLUDED_ICMPBENCHMARKINTERFACE
