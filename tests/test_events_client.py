from __future__ import annotations

import unittest

from geofetch.providers.events.client import CemsClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.headers = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(next(self.pages))


class CemsClientTests(unittest.TestCase):
    def test_limit_stops_after_requested_events(self) -> None:
        session = FakeSession(
            [
                {
                    "results": [{"code": "EMSR3"}, {"code": "EMSR2"}, {"code": "EMSR1"}],
                    "next": None,
                }
            ]
        )
        client = CemsClient(session=session)

        results = client.list_activations(limit=2)

        self.assertEqual([item["code"] for item in results], ["EMSR3", "EMSR2"])
        self.assertEqual(session.calls[0][1]["params"]["limit"], 20)

    def test_limit_zero_follows_pagination(self) -> None:
        next_url = (
            "http://mapping.emergency.copernicus.eu/"
            "activations/api/activations/?limit=1&offset=1"
        )
        session = FakeSession(
            [
                {"results": [{"code": "EMSR2"}], "next": next_url},
                {"results": [{"code": "EMSR1"}], "next": None},
            ]
        )
        client = CemsClient(session=session)

        results = client.list_activations(limit=0, page_size=1)

        self.assertEqual([item["code"] for item in results], ["EMSR2", "EMSR1"])
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[1][0].startswith("https://"))

    def test_filters_non_response_activations(self) -> None:
        session = FakeSession(
            [
                {
                    "results": [
                        {"code": "EMSN200", "drmPhase": "recovery"},
                        {"code": "EMSR927", "drmPhase": "response"},
                    ],
                    "next": None,
                }
            ]
        )
        client = CemsClient(session=session)

        results = client.list_activations(limit=0)

        self.assertEqual([item["code"] for item in results], ["EMSR927"])


if __name__ == "__main__":
    unittest.main()
