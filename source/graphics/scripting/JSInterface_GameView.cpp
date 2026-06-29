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

#include "JSInterface_GameView.h"

#include "graphics/Camera.h"
#include "graphics/GameView.h"
#include "graphics/Terrain.h"
#include "maths/Fixed.h"
#include "maths/FixedVector3D.h"
#include "maths/Vector3D.h"
#include "ps/CLogger.h"
#include "ps/Game.h"
#include "ps/World.h"
#include "scriptinterface/FunctionWrapper.h"
#include "scriptinterface/Object.h"
#include "scriptinterface/Request.h"
#include "simulation2/helpers/Position.h"
#include "simulation2/system/Entity.h"

#include <js/RootingAPI.h>
#include <js/TypeDecls.h>
#include <js/Value.h>

namespace JSI_GameView
{
#define IMPLEMENT_BOOLEAN_SCRIPT_SETTING(NAME) \
bool Get##NAME##Enabled() \
{ \
	if (!g_Game || !g_Game->GetView()) \
	{ \
		LOGERROR("Trying to get a setting from GameView when it's not initialized!"); \
		return false; \
	} \
	return g_Game->GetView()->Get##NAME##Enabled(); \
} \
\
void Set##NAME##Enabled(bool Enabled) \
{ \
	if (!g_Game || !g_Game->GetView()) \
	{ \
		LOGERROR("Trying to set a setting of GameView when it's not initialized!"); \
		return; \
	} \
	g_Game->GetView()->Set##NAME##Enabled(Enabled); \
}

IMPLEMENT_BOOLEAN_SCRIPT_SETTING(Culling);
IMPLEMENT_BOOLEAN_SCRIPT_SETTING(LockCullCamera);
IMPLEMENT_BOOLEAN_SCRIPT_SETTING(ConstrainCamera);

#undef IMPLEMENT_BOOLEAN_SCRIPT_SETTING


#define REGISTER_BOOLEAN_SCRIPT_SETTING(NAME) \
	Script::Function::Register<&Get##NAME##Enabled>(rq, "GameView_Get" #NAME "Enabled"); \
	Script::Function::Register<&Set##NAME##Enabled>(rq, "GameView_Set" #NAME "Enabled");

void RegisterScriptFunctions_Settings(const Script::Request& rq)
{
	REGISTER_BOOLEAN_SCRIPT_SETTING(Culling);
	REGISTER_BOOLEAN_SCRIPT_SETTING(LockCullCamera);
	REGISTER_BOOLEAN_SCRIPT_SETTING(ConstrainCamera);
}

#undef REGISTER_BOOLEAN_SCRIPT_SETTING

JS::Value GetCameraRotation(const Script::Request& rq)
{
	if (!g_Game || !g_Game->GetView())
		return JS::UndefinedValue();

	const CVector3D rotation = g_Game->GetView()->GetCameraRotation();
	JS::RootedValue val(rq.cx);
	Script::CreateObject(rq, &val, "x", rotation.X, "y", rotation.Y);
	return val;
}

JS::Value GetCameraZoom()
{
	if (!g_Game || !g_Game->GetView())
		return JS::UndefinedValue();
	return JS::NumberValue(g_Game->GetView()->GetCameraZoom());
}

JS::Value GetCameraPivot(const Script::Request& rq)
{
	if (!g_Game || !g_Game->GetView())
		return JS::UndefinedValue();

	const CVector3D pivot = g_Game->GetView()->GetCameraPivot();
	JS::RootedValue pivotValue(rq.cx);
	Script::CreateObject(rq, &pivotValue, "x", pivot.X, "z", pivot.Z);
	return pivotValue;
}

JS::Value GetCameraPosition(const Script::Request& rq)
{
	if (!g_Game || !g_Game->GetView())
		return JS::UndefinedValue();

	const CVector3D position = g_Game->GetView()->GetCameraPosition();
	JS::RootedValue positionValue(rq.cx);
	Script::CreateObject(rq, &positionValue, "x", position.X, "y", position.Y, "z", position.Z);
	return positionValue;
}

/**
 * Reload camera configuration
 */
void LoadCameraConfig()
{
    if (!g_Game || !g_Game->GetView())
        return;
    g_Game->GetView()->LoadCameraConfig();
}

/**
 * Move camera to a 2D location.
 */
void CameraMoveTo(entity_pos_t x, entity_pos_t z)
{
	if (!g_Game || !g_Game->GetWorld() || !g_Game->GetView())
		return;

	const CTerrain& terrain = g_Game->GetWorld()->GetTerrain();

	CVector3D target;
	target.X = x.ToFloat();
	target.Z = z.ToFloat();
	target.Y = terrain.GetExactGroundLevel(target.X, target.Z);

	g_Game->GetView()->MoveCameraTarget(target);
}

/**
 * Set the camera to look at the given location.
 */
void SetCameraTarget(float x, float y, float z)
{
	if (!g_Game || !g_Game->GetView())
		return;
	g_Game->GetView()->MoveCameraTarget(CVector3D(x, y, z));
}

/**
 * Set the data (position, orientation and zoom) of the camera.
 */
void SetCameraData(entity_pos_t x, entity_pos_t y, entity_pos_t z, entity_pos_t rotx, entity_pos_t roty, entity_pos_t zoom)
{
	if (!g_Game || !g_Game->GetView())
		return;

	CVector3D pos(x.ToFloat(), y.ToFloat(), z.ToFloat());

	g_Game->GetView()->SetCamera(pos, rotx.ToFloat(), roty.ToFloat(), zoom.ToFloat());
}

/**
 * Start / stop camera following mode.
 * @param entityid unit id to follow. If zero, stop following mode
 */
void CameraFollow(entity_id_t entityid)
{
	if (!g_Game || !g_Game->GetView())
		return;

	g_Game->GetView()->FollowEntity(entityid, false);
}

/**
 * Start / stop first-person camera following mode.
 * @param entityid unit id to follow. If zero, stop following mode.
 */
void CameraFollowFPS(entity_id_t entityid)
{
	if (!g_Game || !g_Game->GetView())
		return;

	g_Game->GetView()->FollowEntity(entityid, true);
}

entity_id_t GetFollowedEntity()
{
	if (!g_Game || !g_Game->GetView())
		return INVALID_ENTITY;

	return g_Game->GetView()->GetFollowedEntity();
}

CFixedVector3D GetTerrainAtScreenPoint(int x, int y)
{
	CVector3D pos = g_Game->GetView()->GetCamera().GetWorldCoordinates(x, y, true);
	return CFixedVector3D(fixed::FromFloat(pos.X), fixed::FromFloat(pos.Y), fixed::FromFloat(pos.Z));
}

void RegisterScriptFunctions(const Script::Request& rq)
{
	RegisterScriptFunctions_Settings(rq);

	Script::Function::Register<&GetCameraRotation>(rq, "GetCameraRotation");
	Script::Function::Register<&GetCameraZoom>(rq, "GetCameraZoom");
	Script::Function::Register<&GetCameraPivot>(rq, "GetCameraPivot");
	Script::Function::Register<&GetCameraPosition>(rq, "GetCameraPosition");
	Script::Function::Register<&CameraMoveTo>(rq, "CameraMoveTo");
	Script::Function::Register<&LoadCameraConfig>(rq, "LoadCameraConfig");
	Script::Function::Register<&SetCameraTarget>(rq, "SetCameraTarget");
	Script::Function::Register<&SetCameraData>(rq, "SetCameraData");
	Script::Function::Register<&CameraFollow>(rq, "CameraFollow");
	Script::Function::Register<&CameraFollowFPS>(rq, "CameraFollowFPS");
	Script::Function::Register<&GetFollowedEntity>(rq, "GetFollowedEntity");
	Script::Function::Register<&GetTerrainAtScreenPoint>(rq, "GetTerrainAtScreenPoint");
}
}
