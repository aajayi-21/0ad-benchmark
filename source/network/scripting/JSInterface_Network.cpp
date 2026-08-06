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

#include "JSInterface_Network.h"

#include "lib/code_generation.h"
#include "lib/debug.h"
#include "lib/types.h"
#include "lib/utf8.h"
#include "lobby/XmppClient.h"
#include "network/NetClient.h"
#include "network/NetMessage.h"
#include "network/NetServer.h"
#include "ps/CLogger.h"
#include "ps/CStr.h"
#include "ps/GUID.h"
#include "ps/Game.h"
#include "ps/Hashing.h"
#include "ps/Pyrogenesis.h"
#include "ps/SavedGame.h"
#include "scriptinterface/FunctionWrapper.h"
#include "scriptinterface/JSON.h"
#include "scriptinterface/Conversions.h"
#include "scriptinterface/Request.h"
#include "scriptinterface/StructuredClone.h"

#include <fmt/format.h>
#include <js/PropertyAndElement.h>
#include <js/RootingAPI.h>
#include <js/TypeDecls.h>
#include <js/Value.h>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace Script { class Interface; }

namespace JSI_Network
{
u16 GetDefaultPort()
{
	return PS_DEFAULT_PORT;
}

bool IsNetController()
{
	return !!g_NetClient && g_NetClient->IsController();
}

bool HasNetServer()
{
	return !!g_NetServer;
}

bool HasNetClient()
{
	return !!g_NetClient;
}

void StartNetworkHost(const CStrW& playerName, const u16 serverPort, const CStr& password,
	const bool continueSavedGame, bool storeReplay)
{
	ENSURE(!g_NetClient);
	ENSURE(!g_NetServer);
	ENSURE(!g_Game);

	// Always use lobby authentication for lobby matches to prevent impersonation and smurfing, in particular through mods that implemented an UI for arbitrary or other players nicknames.
	const bool hasLobby = !!g_XmppClient;
	const std::string hostJID{hasLobby ? g_XmppClient->GetJID() : ""};

	/**
	 * Password security - we want 0 A.D. to protect players from malicious hosts. We assume that clients
	 * might mistakenly send a personal password instead of the game password (e.g. enter their mail account's password on autopilot).
	 * Malicious dedicated servers might be set up to farm these failed logins and possibly obtain user credentials.
	 * Therefore, we hash the passwords on the client side before sending them to the server.
	 * This still makes the passwords potentially recoverable, but makes it much harder at scale.
	 * To prevent the creation of rainbow tables, hash with:
	 * - the host name
	 * - the client name (this makes rainbow tables completely unworkable unless a specific user is targeted,
	 *   but that would require both computing the matching rainbow table _and_ for that specific user to mistype a personal password,
	 *   at which point we assume the attacker would/could probably just rather use another means of obtaining the password).
	 * - the password itself
	 * - the engine version (so that the hashes change periodically)
	 * TODO: it should be possible to implement SRP or something along those lines to completely protect from this,
	 * but the cost/benefit ratio is probably not worth it.
	 */
	std::string hashedPassword = hasLobby ?
		HashCryptographically(password, hostJID + password + PS_SERIALIZATION_VERSION) : "";

	const std::string secret = ps_generate_guid();
	g_NetServer = new CNetServer(continueSavedGame, serverPort, hasLobby, hashedPassword, secret);

	// Generate a secret to identify the host client.

	g_Game = new CGame(storeReplay);
	g_NetClient = new CNetClient(g_Game, "127.0.0.1", serverPort, playerName, hostJID, hashedPassword,
		secret);
}

void StartNetworkJoin(const CStrW& playerName, const CStr& serverAddress, u16 serverPort, bool storeReplay)
{
	ENSURE(!g_NetClient);
	ENSURE(!g_NetServer);
	ENSURE(!g_Game);

	g_Game = new CGame(storeReplay);
	g_NetClient = new CNetClient(g_Game, serverAddress, serverPort, playerName);
}

/**
 * Requires XmppClient to send iq request to the server to get server's ip and port based on passed password.
 * This is needed to not force server to share it's public ip with all potential clients in the lobby.
 * XmppClient will also handle logic after receiving the answer.
 */
void StartNetworkJoinLobby(const CStrW& playerName, const CStr& hostJID, const CStr& password)
{
	ENSURE(!!g_XmppClient);
	ENSURE(!g_NetClient);
	ENSURE(!g_NetServer);
	ENSURE(!g_Game);

	CStr hashedPass = HashCryptographically(password, hostJID + password + PS_SERIALIZATION_VERSION);
	g_Game = new CGame(true);
	g_NetClient = new CNetClient(g_Game, playerName, hostJID, hashedPass, *g_XmppClient);
}

void DisconnectNetworkGame()
{
	// TODO: we ought to do async reliable disconnections

	SAFE_DELETE(g_NetServer);
	SAFE_DELETE(g_NetClient);
	SAFE_DELETE(g_Game);
}

CStr GetPlayerGUID()
{
	if (!g_NetClient)
		return "local";

	return g_NetClient->GetGUID();
}

JS::Value PollNetworkClient(const Script::Interface& guiInterface)
{
	if (!g_NetClient)
		throw std::logic_error{"Network client not present"};

	return JS::ObjectValue(*g_NetClient->GetNextGUIMessage(guiInterface));
}

void SendGameSetupMessage(const Script::Interface& scriptInterface, JS::HandleValue attribs1)
{
	ENSURE(g_NetClient);

	// TODO: This is a workaround because we need to pass a MutableHandle to a JSAPI functions somewhere (with no obvious reason).
	Script::Request rq(scriptInterface);
	JS::RootedValue attribs(rq.cx, attribs1);

	g_NetClient->SendGameSetupMessage(&attribs, scriptInterface);
}

void AssignNetworkPlayer(int playerID, const CStr& guid)
{
	ENSURE(g_NetClient);

	g_NetClient->SendAssignPlayerMessage(playerID, guid);
}

void KickPlayer(const CStrW& playerName, bool ban)
{
	if (!g_NetClient)
		throw std::logic_error{"g_NetClient is null."};
	g_NetClient->SendKickPlayerMessage(playerName, ban);
}

void SendNetworkChat(const Script::Request& rq, const CStrW& message, JS::HandleValue handle)
{
	ENSURE(g_NetClient);

	if (handle.isNullOrUndefined())
	{
		g_NetClient->SendChatMessage(message, std::nullopt);
		return;
	}

	auto receivers = std::make_optional<std::vector<std::string>>();
	if (!Script::FromJSVal(rq, handle, *receivers))
	{
		throw std::invalid_argument{"The second argument to `SendNetworkChat` has to be either an "
			"Array or a nullish value."};
		return;
	}

	g_NetClient->SendChatMessage(message, std::move(receivers));
}

void SendNetworkReady(int message)
{
	ENSURE(g_NetClient);

	g_NetClient->SendReadyMessage(message);
}

void ClearAllPlayerReady ()
{
	ENSURE(g_NetClient);

	g_NetClient->SendClearAllReadyMessage();
}

void StartNetworkGame(const Script::Interface& scriptInterface, JS::HandleValue savegame, JS::HandleValue attribs1)
{
	ENSURE(g_NetClient);

	Script::Request rq(scriptInterface);

	JS::RootedValue attribs(rq.cx, attribs1);
	std::string attributesAsString{Script::StringifyJSON(rq, &attribs)};

	if (savegame.isUndefined())
	{
		g_NetClient->SendStartGameMessage(attributesAsString);
		return;
	}

	std::wstring savegameID;
	Script::FromJSVal(rq, savegame, savegameID);

	const std::optional<SavedGames::LoadResult> loadResult{SavedGames::Load(scriptInterface, savegameID)};
	if (loadResult)
		g_NetClient->SendStartSavedGameMessage(attributesAsString, loadResult->savedState);
	else
	{
		throw std::runtime_error{fmt::format("Failed to load the saved game: \"{}\"",
			utf8_from_wstring(savegameID).c_str())};
	}
}

void SetTurnLength(int length)
{
	if (g_NetServer)
		g_NetServer->SetTurnLength(length);
	else
		LOGERROR("Only network host can change turn length");
}

void SendNetworkFlare(JS::HandleValue position)
{
	ENSURE(g_NetClient);

	Script::Request rq(g_NetClient->GetScriptInterface());
	ENSURE(position.isObject());
	JS::RootedObject positionObj(rq.cx, &position.toObject());
	JS::RootedValue positionX(rq.cx);
	JS::RootedValue positionY(rq.cx);
	JS::RootedValue positionZ(rq.cx);
	ENSURE(JS_GetProperty(rq.cx, positionObj, "x", &positionX));
	ENSURE(JS_GetProperty(rq.cx, positionObj, "y", &positionY));
	ENSURE(JS_GetProperty(rq.cx, positionObj, "z", &positionZ));

	// (TODO?): Converting the doubles into strings here is a workaround because direct (de)serialisation of floating point numbers is not supported.
	// It causes somewhat awkward message handling, but the resulting efficiency losses are negligible.
	g_NetClient->SendFlareMessage(
		fmt::format("{}", positionX.toNumber()),
		fmt::format("{}", positionY.toNumber()),
		fmt::format("{}", positionZ.toNumber())
	);
}

void RegisterScriptFunctions(const Script::Request& rq)
{
	Script::Function::Register<&GetDefaultPort>(rq, "GetDefaultPort");
	Script::Function::Register<&IsNetController>(rq, "IsNetController");
	Script::Function::Register<&HasNetServer>(rq, "HasNetServer");
	Script::Function::Register<&HasNetClient>(rq, "HasNetClient");
	Script::Function::Register<&StartNetworkHost>(rq, "StartNetworkHost");
	Script::Function::Register<&StartNetworkJoin>(rq, "StartNetworkJoin");
	Script::Function::Register<&StartNetworkJoinLobby>(rq, "StartNetworkJoinLobby");
	Script::Function::Register<&DisconnectNetworkGame>(rq, "DisconnectNetworkGame");
	Script::Function::Register<&GetPlayerGUID>(rq, "GetPlayerGUID");
	Script::Function::Register<&PollNetworkClient>(rq, "PollNetworkClient");
	Script::Function::Register<&SendGameSetupMessage>(rq, "SendGameSetupMessage");
	Script::Function::Register<&AssignNetworkPlayer>(rq, "AssignNetworkPlayer");
	Script::Function::Register<&KickPlayer>(rq, "KickPlayer");
	Script::Function::Register<&SendNetworkChat>(rq, "SendNetworkChat");
	Script::Function::Register<&SendNetworkReady>(rq, "SendNetworkReady");
	Script::Function::Register<&ClearAllPlayerReady>(rq, "ClearAllPlayerReady");
	Script::Function::Register<&StartNetworkGame>(rq, "StartNetworkGame");
	Script::Function::Register<&SetTurnLength>(rq, "SetTurnLength");
	Script::Function::Register<&SendNetworkFlare>(rq, "SendNetworkFlare");
}
}
