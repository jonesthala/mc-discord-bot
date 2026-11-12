import threading
import unittest

from bot.core import McError
from bot.versions import (
    CACHE_SECONDS,
    MOJANG_URL,
    PAPER_URL,
    VersionCatalog,
    paper_releases,
    vanilla_releases,
)

PAPER = {
    "project": {"id": "paper"},
    "versions": {
        "26.3": ["26.3", "26.3-rc-3"],
        "26.1": ["26.1.2", "26.1.1"],
        "1.21": ["1.21.11", "1.21.11-pre5", "1.21.10", "1.21.4"],
    },
}
MOJANG = {
    "latest": {"release": "26.3", "snapshot": "26.4-snapshot-8"},
    "versions": [
        {"id": "26.4-snapshot-8", "type": "snapshot"},
        {"id": "26.3", "type": "release"},
        {"id": "26.2", "type": "release"},
        {"id": "1.21.11", "type": "release"},
        {"id": "b1.7.3", "type": "old_beta"},
    ],
}


class FakeApi:
    def __init__(self):
        self.calls = []
        self.down = False

    def __call__(self, url):
        self.calls.append(url)
        if self.down:
            raise OSError("network down")
        return {PAPER_URL: PAPER, MOJANG_URL: MOJANG}[url]


class ParseTest(unittest.TestCase):
    def test_paper_only_stable_newest_first(self):
        self.assertEqual(paper_releases(PAPER), ["26.3", "26.1.2", "26.1.1", "1.21.11", "1.21.10", "1.21.4"])

    def test_vanilla_only_releases(self):
        self.assertEqual(vanilla_releases(MOJANG), ["26.3", "26.2", "1.21.11"])

    def test_garbage(self):
        for bad in (None, [], {"versions": "x"}, {"versions": {"a": "b"}}):
            self.assertEqual(paper_releases(bad), [])
            self.assertEqual(vanilla_releases(bad), [])


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.api = FakeApi()
        self.catalog = VersionCatalog(fetch=self.api, clock=lambda: self.now[0])

    def test_cache(self):
        self.catalog.releases("paper")
        self.catalog.releases("paper")
        self.assertEqual(len(self.api.calls), 1)
        self.now[0] += CACHE_SECONDS + 1
        self.catalog.releases("paper")
        self.assertEqual(len(self.api.calls), 2)

    def test_unknown_type(self):
        self.assertEqual(self.catalog.releases("forge"), [])
        self.assertEqual(self.api.calls, [])

    def test_api_down_keeps_old_list(self):
        self.assertEqual(self.catalog.releases("vanilla")[0], "26.3")
        self.api.down = True
        self.now[0] += CACHE_SECONDS + 1
        self.assertEqual(self.catalog.releases("vanilla")[0], "26.3")

    def test_api_down_without_cache(self):
        self.api.down = True
        self.assertEqual(self.catalog.releases("paper"), [])
        with self.assertRaises(McError):
            self.catalog.resolve("paper", "neueste")
        self.assertEqual(self.catalog.resolve("paper", "1.21.4"), "1.21.4")  # format still checked

    def test_stale_list_is_returned_at_once(self):
        self.catalog.releases("paper")
        self.now[0] += CACHE_SECONDS + 1
        done = threading.Event()
        original = self.catalog._refresh

        def slow_refresh(kind):
            result = original(kind)
            done.set()
            return result

        self.catalog._refresh = slow_refresh
        self.assertEqual(self.catalog.releases("paper", allow_stale=True)[0], "26.3")
        self.assertTrue(done.wait(2))

    def test_resolve(self):
        self.assertEqual(self.catalog.resolve("paper", "Neueste"), "26.3")
        self.assertEqual(self.catalog.resolve("paper", ""), "26.3")
        self.assertEqual(self.catalog.resolve("paper", " 1.21.4 "), "1.21.4")
        self.assertEqual(self.catalog.resolve("vanilla", "26.2"), "26.2")
        for bad in ("26.2", "snapshot", "1.21.11-pre5", "abc"):
            with self.assertRaises(McError, msg=bad):
                self.catalog.resolve("paper", bad)

    def test_choices(self):
        options = self.catalog.choices("paper", "")
        self.assertEqual(options[0], ("Neueste: 26.3", "26.3"))
        self.assertEqual([value for _, value in options].count("26.3"), 1)
        self.assertEqual([value for _, value in self.catalog.choices("paper", "1.21")],
                         ["1.21.11", "1.21.10", "1.21.4"])
        self.assertEqual(self.catalog.choices("paper", "neu")[0][1], "26.3")
        self.assertEqual(self.catalog.choices("vanilla", "9"), [])

    def test_choices_limit(self):
        many = {"versions": {"1": [f"1.{i}.0" for i in range(60)]}}
        catalog = VersionCatalog(fetch=lambda url: many, clock=lambda: 0.0)
        self.assertEqual(len(catalog.choices("paper", "")), 25)


if __name__ == "__main__":
    unittest.main()
