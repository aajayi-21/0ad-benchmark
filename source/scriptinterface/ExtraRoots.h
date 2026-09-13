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

#ifndef INCLUDED_SCRIPTINTERFACE_EXTRAROOTS
#define INCLUDED_SCRIPTINTERFACE_EXTRAROOTS

#include <js/GCAPI.h>

namespace Script
{

/**
 * Register a tracer for externally held GC roots, keyed by (callback, data).
 *
 * This replaces direct calls to JS_AddExtraGCRootsTracer / JS_RemoveExtraGCRootsTracer for
 * callers that register one callback many times with different data (every GUI object with a
 * script handler does). SpiderMonkey 128 before the fix for Mozilla bug 1982134, which is what a
 * system libmozjs-128 provides, removes the first tracer with the same callback regardless of
 * data, so a destroyed object's tracer stayed registered and dereferenced freed memory at the
 * next collection. The engine keeps the pairs itself behind a single tracer per context whose
 * callback nobody else uses.
 */
void AddExtraGCRootsTracer(JSContext* cx, JSTraceDataOp op, void* data);

/** Undo AddExtraGCRootsTracer for exactly this (callback, data) pair. */
void RemoveExtraGCRootsTracer(JSContext* cx, JSTraceDataOp op, void* data);

/** Called by Script::Context when it creates and destroys its JSContext. */
void InitExtraGCRoots(JSContext* cx);
void ShutdownExtraGCRoots(JSContext* cx);

} // namespace Script

#endif // INCLUDED_SCRIPTINTERFACE_EXTRAROOTS
