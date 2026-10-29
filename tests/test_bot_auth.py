from types import SimpleNamespace

import pytest

from bot.ui import OwnerOnly


@pytest.mark.parametrize('user,chat,expected', [(123, 'private', True),
                                              (456, 'private', False), (123, 'group', False)])
async def test_owner_and_private_chat_required(user, chat, expected):
    called = False
    async def handler(event, data):
        nonlocal called
        called = True
    event = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(type=chat))
    await OwnerOnly(123)(handler, event, {})
    assert called is expected
