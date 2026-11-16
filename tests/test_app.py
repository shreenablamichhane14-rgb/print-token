"""Starting the gateway when Telegram cannot be reached yet (a Pi that has just booted, a laptop
that has just woken up)."""
import asyncio

import httpx
import pytest

from printtoken.app import wait_for_telegram
from printtoken.telegram import TelegramError


class FlakyTG:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    async def call(self, method, **params):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return {"username": "print_token_bot"}


def test_start_waits_for_the_network_instead_of_crashing():
    tg = FlakyTG([httpx.ConnectError("name resolution failed"), TelegramError(502, "Bad Gateway")])
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    me = asyncio.run(wait_for_telegram(tg, first_delay=5, sleep=sleep))
    assert me["username"] == "print_token_bot"
    assert tg.calls == 3 and waits == [5, 10]


def test_a_rejected_token_still_stops_the_start():
    tg = FlakyTG([TelegramError(401, "Unauthorized")])

    async def sleep(seconds):
        raise AssertionError("must not wait on a bad token")

    with pytest.raises(TelegramError):
        asyncio.run(wait_for_telegram(tg, sleep=sleep))
