"""Registered MCP tools expose notification state without reading message history."""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from telethon import functions, types

from telegram_mcp.tools import chats as main

NOW = 1800000000


@pytest.fixture
def api(monkeypatch):
    entity = types.Channel(
        id=123,
        title="Fixture",
        photo=types.ChatPhotoEmpty(),
        date=None,
        access_hash=456,
        megagroup=True,
    )
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChannel(123, 456)
    monkeypatch.setattr(main, "get_client", lambda account=None: client)
    monkeypatch.setattr(main, "ensure_connected", AsyncMock())
    monkeypatch.setattr(
        main,
        "format_tool_result",
        lambda records, metadata=None: json.dumps(metadata if metadata is not None else records),
    )
    monkeypatch.setattr(main, "resolve_entity", AsyncMock(return_value=entity))
    monkeypatch.setattr(main.time, "time", lambda: NOW)
    return NS(entity=entity, client=client)


async def call(name, **args):
    return await main.mcp._tool_manager.get_tool(name).run(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "until,muted",
    [(0, False), (NOW - 1, False), (NOW, False), (NOW + 1, True), (2147483647, True)],
)
async def test_explicit_mute_and_expiry_without_history(api, until, muted):
    api.client.return_value = types.PeerNotifySettings(
        mute_until=datetime.fromtimestamp(until, timezone.utc)
    )
    result = json.loads(await call("get_chat_notification_settings", chat_id=-1000000000123))
    assert result == dict(
        chat_id=-1000000000123, muted=muted, mute_until=until, peer_mute_until=until, source="chat"
    )
    assert isinstance(api.client.call_args.args[0], functions.account.GetNotifySettingsRequest)
    api.client.assert_awaited_once()
    api.client.get_messages.assert_not_awaited()
    api.client.get_dialogs.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,peer_type",
    [
        ("user", types.InputNotifyUsers),
        ("group", types.InputNotifyChats),
        ("broadcast", types.InputNotifyBroadcasts),
    ],
)
async def test_inherited_mute_category(api, kind, peer_type):
    if kind == "user":
        api.entity = types.User(id=123, first_name="Fixture")
        main.resolve_entity.return_value = api.entity
    elif kind == "broadcast":
        api.entity.broadcast = True
        api.entity.megagroup = False
    api.client.side_effect = [
        types.PeerNotifySettings(),
        types.PeerNotifySettings(mute_until=NOW + 60),
    ]
    result = json.loads(await call("get_chat_notification_settings", chat_id="@fixture"))
    assert result["muted"] is True and result["mute_until"] == NOW + 60
    assert result["peer_mute_until"] is None
    assert isinstance(api.client.call_args_list[1].args[0].peer, peer_type)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_missing_or_failed_settings_stay_unknown(api, failure):
    api.client.side_effect = [
        types.PeerNotifySettings(),
        RuntimeError("fixture") if failure else types.PeerNotifySettings(),
    ]
    result = json.loads(await call("get_chat_notification_settings", chat_id="@fixture"))
    assert result["muted"] is None and result["mute_until"] is None
    assert result.get("error") == ("RuntimeError" if failure else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_chats", "list_chats"])
async def test_lists_expose_mute_reuse_defaults_and_respect_zero(api, tool):
    api.client.get_dialogs.return_value = [
        NS(
            entity=api.entity,
            unread_count=0,
            dialog=NS(notify_settings=types.PeerNotifySettings(mute_until=until)),
        )
        for until in [None, None, 0]
    ]
    api.client.return_value = types.PeerNotifySettings(mute_until=NOW + 60)
    result = await call(tool)
    assert result.count('"muted": true') == 2
    assert result.count('"muted": false') == 1
    assert result.count('"source": "default:groups"') == 2
    api.client.assert_awaited_once()
    api.client.get_messages.assert_not_awaited()
    api.client.get_input_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_chat_exposes_notification_state(api, monkeypatch):
    def raise_error(name, error, **kwargs):
        raise error

    monkeypatch.setattr(main, "log_and_format_error", raise_error)
    api.client.return_value = types.PeerNotifySettings(mute_until=NOW + 60)
    api.client.get_participants.return_value = NS(total=1)
    api.client.get_dialogs.return_value = []
    result = await call("get_chat", chat_id="@fixture")
    assert '"muted": true' in result and '"source": "chat"' in result


@pytest.mark.asyncio
async def test_default_failure_is_cached_but_list_survives(api):
    api.client.get_dialogs.return_value = [
        NS(
            entity=api.entity,
            unread_count=0,
            dialog=NS(notify_settings=types.PeerNotifySettings()),
        )
        for _ in range(2)
    ]
    api.client.side_effect = RuntimeError("fixture")
    result = await call("list_chats")
    assert result.count('"muted": null') == 2
    assert '"muted": false' not in result
    api.client.assert_awaited_once()
