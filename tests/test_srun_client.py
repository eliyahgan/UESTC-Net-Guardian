import logging
import unittest

from uestc_srun_autoconnect import (
    PortalProtocolError,
    SrunClient,
    build_srun_material,
    parse_json_or_jsonp,
)


class SrunAlgorithmTests(unittest.TestCase):
    def test_portal_javascript_vector(self):
        token = "0123456789abcdef" * 4
        hmd5, info, checksum = build_srun_material(
            "testuser",
            "testpass",
            "10.1.2.3",
            "1",
            token,
        )
        self.assertEqual(hmd5, "40fc04063af6053bd63a578b42274cb0")
        self.assertEqual(
            info,
            "{SRBX1}5uWiKejMz2+plGClEcKSw3Z4I7w/8zy8Cu7G/RX8yB4dDeWDhsc+ovh9+"
            "lnsjS2lzOo/9QxTQjLtSDy3GGnxbCy3QIt6JZRokTYXat2j70loPvMbMxtHd9hky+"
            "dC7oz1xPde4S==",
        )
        self.assertEqual(checksum, "363a28adecec6e5b9a3cb00e34d9407ab9767712")

    def test_json_and_jsonp(self):
        self.assertEqual(parse_json_or_jsonp('{"error":"ok"}')["error"], "ok")
        self.assertEqual(
            parse_json_or_jsonp('cb({"error":"ok"});', expected_callback="cb")["error"],
            "ok",
        )
        with self.assertRaises(PortalProtocolError):
            parse_json_or_jsonp('other({"error":"ok"});', expected_callback="cb")


class OnlineStateTests(unittest.TestCase):
    @staticmethod
    def make_client(payload):
        client = SrunClient.__new__(SrunClient)
        client.logger = logging.getLogger("srun-test")
        client._request_json = lambda *args, **kwargs: payload
        return client

    def test_portal_online_is_authoritative(self):
        self.assertIs(self.make_client({"error": "ok"}).query_online_state(), True)

    def test_portal_offline_overrides_ping_assumptions(self):
        self.assertIs(
            self.make_client({"error": "not_online_error"}).query_online_state(),
            False,
        )


if __name__ == "__main__":
    unittest.main()
