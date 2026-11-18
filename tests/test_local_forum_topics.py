"""Exercise registered forum tools with real Telethon requests and updates."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telethon import errors, functions, types, utils

from telegram_mcp.tools import chats as main


@pytest.fixture
def forum(monkeypatch):
    entity = types.Channel(
        id=123,
        title="Family",
        photo=types.ChatPhotoEmpty(),
        date=None,
        access_hash=456,
        megagroup=True,
        forum=True,
    )
    client = AsyncMock()
    client.get_input_entity.return_value = types.InputPeerChannel(123, 456)

    async def respond(request):
        updates = []
        if isinstance(request, functions.messages.CreateForumTopicRequest):
            updates = [
                types.UpdateMessageID(id=999, random_id=request.random_id + 1),
                types.UpdateMessageID(id=42, random_id=request.random_id),
            ]
        return types.Updates(updates, [], [], datetime.now(timezone.utc), 1)

    client.side_effect = respond
    monkeypatch.setattr(main, "get_client", lambda account=None: client)
    monkeypatch.setattr(main, "resolve_entity", AsyncMock(return_value=entity))
    return SimpleNamespace(entity=entity, client=client)


async def call_tool(name, **args):
    tool = main.mcp._tool_manager.get_tool(name)
    assert tool is not None
    return await tool.run({"chat_id": "@family_chat", **args})


@pytest.mark.asyncio
async def test_enable_forum_sends_serializable_request(forum):
    forum.entity.forum = False
    assert "Forum topics enabled" in await call_tool("enable_forum")
    forum.client.assert_awaited_once()
    request = forum.client.call_args.args[0]
    assert isinstance(request, functions.channels.ToggleForumRequest)
    assert request.channel is forum.entity
    assert request.enabled is True and request.tabs is False
    await request.resolve(forum.client, utils)
    assert bytes(request)


@pytest.mark.asyncio
async def test_enable_existing_forum_preserves_settings(forum):
    forum.entity.forum_tabs = True
    assert "already enabled" in await call_tool("enable_forum")
    forum.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Поездки", "я" * 64])
async def test_create_topic_returns_matching_id_and_serializable_request(forum, title):
    result = await call_tool("create_topic", title=title)
    assert result == f"Topic '{title}' created with ID: 42 in chat @family_chat."
    forum.client.assert_awaited_once()
    request = forum.client.call_args.args[0]
    assert isinstance(request, functions.messages.CreateForumTopicRequest)
    assert request.peer is forum.entity and request.title == title
    await request.resolve(forum.client, utils)
    assert bytes(request)


@pytest.mark.asyncio
async def test_create_without_id_does_not_invent_one(forum):
    forum.client.side_effect = None
    forum.client.return_value = types.UpdatesTooLong()
    result = await call_tool("create_topic", title="Gifts")
    assert "Use list_topics" in result
    assert "ID:" not in result
    forum.client.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["", " \n ", "a" * 129, "я" * 65])
async def test_invalid_title_never_sends(forum, title):
    assert "title must be" in await call_tool("create_topic", title=title)
    forum.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["user", "broadcast", "basic_group"])
@pytest.mark.parametrize("tool", ["enable_forum", "create_topic", "list_topics"])
async def test_non_supergroup_never_sends(forum, kind, tool):
    if kind == "user":
        entity = types.User(id=123)
    elif kind == "basic_group":
        entity = types.Chat(123, "Family", types.ChatPhotoEmpty(), 2, None, 1)
    else:
        entity = forum.entity
        entity.megagroup = False
        entity.broadcast = True
    main.resolve_entity.return_value = entity
    args = {"title": "Trips"} if tool == "create_topic" else {}
    assert "not a supergroup" in await call_tool(tool, chat_id=123, **args)
    forum.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["create_topic", "list_topics"])
async def test_disabled_forum_never_sends(forum, tool):
    forum.entity.forum = False
    args = {"title": "Trips"} if tool == "create_topic" else {}
    result = await call_tool(tool, chat_id=123, **args)
    assert "disabled" in result or "not have forum topics enabled" in result
    forum.client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["enable_forum", "create_topic"])
async def test_permission_error_is_not_success(forum, tool):
    forum.entity.forum = tool == "create_topic"
    forum.client.side_effect = errors.ChatAdminRequiredError(request=None)
    args = {"title": "Trips"} if tool == "create_topic" else {}
    result = await call_tool(tool, **args)
    assert "An error occurred" in result
    assert "created" not in result and "topics enabled" not in result


@pytest.mark.asyncio
async def test_list_topics_uses_current_api_and_keeps_pagination(forum):
    topic = types.ForumTopic(
        id=42,
        date=None,
        peer=types.PeerChannel(123),
        title="Trips",
        icon_color=0x6FB9F0,
        top_message=42,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(1),
        notify_settings=types.PeerNotifySettings(),
    )
    forum.client.side_effect = None
    forum.client.return_value = types.messages.ForumTopics(1, [topic], [], [], [], 0)
    result = await call_tool(
        "list_topics", chat_id=123, limit=5, offset_topic=7, search_query="Tri"
    )
    assert json.loads(result)["results"][0]["id"] == 42
    assert json.loads(result)["results"][0]["title"] == "Trips"
    request = forum.client.call_args.args[0]
    assert isinstance(request, functions.messages.GetForumTopicsRequest)
    assert request.peer is forum.entity
    assert (request.limit, request.offset_topic, request.q) == (5, 7, "Tri")
    await request.resolve(forum.client, utils)
    assert bytes(request)


def test_mutation_tools_are_registered_as_writes():
    for name, idempotent in [("enable_forum", True), ("create_topic", False)]:
        tool = main.mcp._tool_manager.get_tool(name)
        assert tool is not None
        assert not tool.annotations.readOnlyHint
        assert tool.annotations.idempotentHint is idempotent
