import io
import unittest
import urllib.error
from unittest import mock

from bot.core import McError
from bot.players import (
    MOJANG_URL,
    PLAYERDB_URL,
    LookupUnavailable,
    check_account,
    fetch_status,
    lookup_player,
)

PLAYERDB_FOUND = {
    "code": "player.found",
    "message": "Successfully found player by given ID.",
    "data": {"player": {"username": "Steve_01", "id": "1234"}},
    "success": True,
}
PLAYERDB_UNKNOWN = {
    "message": "No Minecraft user could be found.",
    "code": "minecraft.invalid_username",
    "data": {},
    "success": False,
}
MOJANG_FOUND = {"id": "1234", "name": "Steve_01"}
MOJANG_UNKNOWN = {"path": "/users/profiles/minecraft/x", "errorMessage": "Couldn't find any profile"}


class FakeApi:
    """Answers per host; an exception instance is raised instead of returned."""

    def __init__(self, playerdb, mojang=OSError("Mojang not expected")):
        self.answers = {"playerdb.co": playerdb, "api.mojang.com": mojang}
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        answer = self.answers[url.split("/")[2]]
        if isinstance(answer, Exception):
            raise answer
        return answer


class LookupTest(unittest.TestCase):
    def test_found_returns_exact_spelling(self):
        api = FakeApi((200, PLAYERDB_FOUND))
        self.assertEqual(lookup_player("steve_01", fetch=api), "Steve_01")
        self.assertEqual(api.calls, [PLAYERDB_URL.format(name="steve_01")])

    def test_unknown_at_playerdb_counts_without_asking_mojang(self):
        api = FakeApi((400, PLAYERDB_UNKNOWN))
        self.assertIsNone(lookup_player("nobody_here", fetch=api))
        self.assertEqual(len(api.calls), 1)

    def test_mojang_when_playerdb_unreachable(self):
        api = FakeApi(OSError("timed out"), (200, MOJANG_FOUND))
        self.assertEqual(lookup_player("STEVE_01", fetch=api), "Steve_01")
        self.assertEqual(api.calls[1], MOJANG_URL.format(name="STEVE_01"))

    def test_mojang_when_playerdb_answers_oddly(self):
        for odd in ((500, {"code": "minecraft.api_failure"}), (429, None), (200, {"code": "x"}), (502, "<html>")):
            with self.subTest(odd=odd):
                self.assertIsNone(lookup_player("nobody_here", fetch=FakeApi(odd, (404, MOJANG_UNKNOWN))))
                self.assertIsNone(lookup_player("nobody_here", fetch=FakeApi(odd, (204, None))))

    def test_other_name_in_answer_is_not_trusted(self):
        other = {**PLAYERDB_FOUND, "data": {"player": {"username": "Alex"}}}
        api = FakeApi((200, other), (200, MOJANG_FOUND))
        self.assertEqual(lookup_player("Steve_01", fetch=api), "Steve_01")
        self.assertEqual(len(api.calls), 2)

    def test_both_down(self):
        api = FakeApi(OSError("no route"), (429, None))
        with self.assertRaises(LookupUnavailable) as caught:
            lookup_player("Steve_01", fetch=api)
        self.assertIn("playerdb.co", str(caught.exception))
        self.assertIn("429", str(caught.exception))


class CheckAccountTest(unittest.TestCase):
    def test_known_name_is_stored_in_exact_spelling(self):
        self.assertEqual(check_account(" steve_01 ", lookup=lambda name: "Steve_01"), ("Steve_01", True))

    def test_unknown_name_is_refused(self):
        with self.assertRaises(McError) as caught:
            check_account("Stevee_01", lookup=lambda name: None)
        message = str(caught.exception)
        self.assertIn("**Stevee_01**", message)
        self.assertIn("gibt es nicht", message)
        self.assertNotIn("ß", message)

    def test_outage_keeps_typed_name_unverified(self):
        def down(name):
            raise LookupUnavailable("both down")

        with self.assertLogs("mcbot.players", "WARNING"):
            self.assertEqual(check_account("Steve_01", lookup=down), ("Steve_01", False))

    def test_bad_format_is_refused_before_any_lookup(self):
        calls = []
        with self.assertRaises(McError):
            check_account("no space", lookup=calls.append)
        self.assertEqual(calls, [])


class FetchStatusTest(unittest.TestCase):
    def test_error_status_is_returned_with_body(self):
        error = urllib.error.HTTPError(
            "https://playerdb.co/x", 400, "Bad Request", {}, io.BytesIO(b'{"code": "minecraft.invalid_username"}')
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertEqual(fetch_status("https://playerdb.co/x"), (400, {"code": "minecraft.invalid_username"}))

    def test_ok_and_empty_body(self):
        for body, expected in ((b'{"name": "Steve"}', {"name": "Steve"}), (b"", None), (b"<html>", None)):
            response = mock.MagicMock(status=200)
            response.read.return_value = body
            response.__enter__.return_value = response
            with self.subTest(body=body), mock.patch("urllib.request.urlopen", return_value=response):
                self.assertEqual(fetch_status("https://api.mojang.com/x"), (200, expected))

    def test_network_errors_are_raised(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(OSError):
                fetch_status("https://playerdb.co/x")


if __name__ == "__main__":
    unittest.main()
