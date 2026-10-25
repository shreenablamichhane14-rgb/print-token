import threading

import pytest

from printtoken.tokens import MintRefused, Tokens, Verdict, is_tag_shaped, new_tag


def test_tag_shape_and_alphabet():
    tags = {new_tag() for _ in range(2000)}
    assert len(tags) == 2000                      # 40 bits, collisions in 2000 draws are vanishingly rare
    for tag in tags:
        assert is_tag_shaped(tag)
        assert not set(tag) & set("01890")        # no characters people misread


def test_mint_then_burn_once(tokens):
    tag, expires = tokens.mint("tg:1", now=1000.0)
    assert expires == 1000.0 + 30 * 60
    assert tokens.check(tag, now=1001) is Verdict.OK
    assert tokens.burn(tag, now=1002) is Verdict.OK
    assert tokens.burn(tag, now=1003) is Verdict.USED      # replay
    assert tokens.check(tag, now=1003) is Verdict.USED


def test_expiry(tokens):
    tag, _ = tokens.mint("tg:1", now=1000.0)
    assert tokens.check(tag, now=1000 + 30 * 60 + 1) is Verdict.EXPIRED
    assert tokens.burn(tag, now=1000 + 30 * 60 + 1) is Verdict.EXPIRED


def test_unknown_and_case_insensitive(tokens):
    assert tokens.check("zzzzzzzz", now=1) is Verdict.UNKNOWN
    tag, _ = tokens.mint("tg:1", now=1000.0)
    assert tokens.burn(tag.upper(), now=1001) is Verdict.OK


def test_outstanding_cap(tokens):
    for _ in range(3):
        tokens.mint("tg:1", now=1000.0)
    with pytest.raises(MintRefused):
        tokens.mint("tg:1", now=1000.0)


def test_hourly_mint_limit(store):
    t = Tokens(store, ttl_min=1, max_outstanding=10, mints_per_hour=3)
    for i in range(3):
        t.mint("tg:1", now=1000.0 + i)
    with pytest.raises(MintRefused):
        t.mint("tg:1", now=1100.0)
    t.mint("tg:1", now=1000.0 + 3601)             # an hour later it works again


def test_only_one_racer_wins(tokens):
    tag, _ = tokens.mint("tg:1", now=1000.0)
    results = []
    barrier = threading.Barrier(8)

    def racer():
        barrier.wait()
        results.append(tokens.burn(tag, now=1001.0))

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count(Verdict.OK) == 1
    assert results.count(Verdict.USED) == 7
