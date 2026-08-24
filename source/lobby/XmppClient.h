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

#ifndef XMPPCLIENT_H
#define XMPPCLIENT_H

#include "lib/types.h"

#include <js/Value.h>
#include <memory>

namespace Script { class Interface; }
namespace Script { class Request; }

class XmppClient
{
public:
	XmppClient(const Script::Interface* scriptInterface, const std::string& username,
		const std::string& password, const std::string& room, const std::string& nick,
		const int historyRequestSize = 0, bool regOpt = false);
	~XmppClient();

	void connect();
	void disconnect();
	bool isConnected();
	void recv();
	void SendIqGetBoardList();
	void SendIqGetProfile(const std::string& player);
	void SendIqGameReport(const Script::Request& rq, JS::HandleValue data);
	void SendIqRegisterGame(const Script::Request& rq, JS::HandleValue data);
	void SendIqGetConnectionData(const std::string& jid, const std::string& password,
		const std::string& clientSalt, bool localIP);
	void SendIqUnregisterGame();
	void SendIqChangeStateGame(const std::string& nbp, const std::string& players);
	void SendIqLobbyAuth(const std::string& to, const std::string& token);
	void SetNick(const std::string& nick);
	std::string GetNick() const;
	std::string GetJID();
	std::string GetUsername() const;
	void ChangePassword(const std::string& newPassword);
	void kick(const std::string& nick, const std::string& reason);
	void ban(const std::string& nick, const std::string& reason);
	void SetPresence(const std::string& presence);
	const char* GetPresence(const std::string& nickname);
	const char* GetRole(const std::string& nickname);
	std::wstring GetRating(const std::string& nickname);
	const std::wstring& GetSubject();
	JS::Value GUIGetPlayerList(const Script::Request& rq);
	JS::Value GUIGetGameList(const Script::Request& rq);
	JS::Value GUIGetBoardList(const Script::Request& rq);
	JS::Value GUIGetProfile(const Script::Request& rq);

	JS::Value GuiPollNewMessages(const Script::Interface& guiInterface);
	JS::Value GuiPollHistoricMessages(const Script::Interface& guiInterface);
	bool GuiPollHasPlayerListUpdate();

	void SendMUCMessage(const std::string& message);
	void SendStunEndpointToHost(const std::string& ip, u16 port, const std::string& hostJID);

private:
	class Impl;
	const std::unique_ptr<Impl> m_Impl;
};

extern XmppClient *g_XmppClient;
extern bool g_rankedGame;

#endif // XMPPCLIENT_H
