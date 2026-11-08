"""MQTT 3.1.1 wire codec: varints, framing, PUBLISH and CONNECT parsing."""
import struct
import unittest

import support
from support import hb


class VarintTests(unittest.TestCase):
    def test_encoding_boundaries(self):
        cases = {
            0: b"\x00",
            127: b"\x7f",
            128: b"\x80\x01",
            16383: b"\xff\x7f",
            16384: b"\x80\x80\x01",
            2097151: b"\xff\xff\x7f",
            2097152: b"\x80\x80\x80\x01",
            268435455: b"\xff\xff\xff\x7f",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(hb.enc_varint(value), expected)

    def test_enc_str_is_length_prefixed_and_accepts_bytes(self):
        self.assertEqual(hb.enc_str("abc"), b"\x00\x03abc")
        self.assertEqual(hb.enc_str(b"abc"), b"\x00\x03abc")
        self.assertEqual(hb.enc_str(""), b"\x00\x00")

    def test_mqtt_packet_layout(self):
        self.assertEqual(hb.mqtt_packet(0xD0), b"\xd0\x00")
        self.assertEqual(hb.mqtt_packet(0x30, b"xy"), b"\x30\x02xy")
        big = hb.mqtt_packet(0x30, b"a" * 200)
        self.assertEqual(big[:3], b"\x30\xc8\x01")  # remaining length 200 = 0xC8 0x01


class StreamParserTests(unittest.TestCase):
    def test_single_packet(self):
        parser = hb.MqttStreamParser()
        pkt = hb.mqtt_packet(0x30, b"hello")
        self.assertEqual(parser.feed(pkt), [(0x30, b"hello")])

    def test_several_packets_in_one_chunk(self):
        parser = hb.MqttStreamParser()
        data = hb.mqtt_packet(0xC0) + hb.mqtt_packet(0x30, b"a") + hb.mqtt_packet(0xE0)
        self.assertEqual(parser.feed(data), [(0xC0, b""), (0x30, b"a"), (0xE0, b"")])

    def test_packet_split_byte_by_byte(self):
        parser = hb.MqttStreamParser()
        pkt = hb.mqtt_packet(0x30, b"x" * 300)  # 2-byte remaining length
        out = []
        for byte in pkt:
            out += parser.feed(bytes([byte]))
        self.assertEqual(out, [(0x30, b"x" * 300)])

    def test_incomplete_packet_is_kept_until_completed(self):
        parser = hb.MqttStreamParser()
        pkt = hb.mqtt_packet(0x30, b"payload")
        self.assertEqual(parser.feed(pkt[:-3]), [])
        self.assertEqual(parser.feed(pkt[-3:]), [(0x30, b"payload")])

    def test_trailing_partial_packet_does_not_lose_the_complete_one(self):
        parser = hb.MqttStreamParser()
        first, second = hb.mqtt_packet(0x30, b"one"), hb.mqtt_packet(0x30, b"two")
        self.assertEqual(parser.feed(first + second[:2]), [(0x30, b"one")])
        self.assertEqual(parser.feed(second[2:]), [(0x30, b"two")])

    def test_malformed_remaining_length_raises(self):
        parser = hb.MqttStreamParser()
        with self.assertRaises(ValueError):
            parser.feed(b"\x30\xff\xff\xff\xff\xff")

    def test_empty_feed(self):
        self.assertEqual(hb.MqttStreamParser().feed(b""), [])


class PublishParsingTests(unittest.TestCase):
    def test_qos0(self):
        pkt = support.publish_packet("invmonitor", b"data")
        _fb, body = hb.MqttStreamParser().feed(pkt)[0]
        self.assertEqual(hb.parse_publish(pkt[0], body), ("invmonitor", 0, None, b"data"))

    def test_qos1_carries_a_packet_id(self):
        pkt = support.publish_packet("invmonitor", b"data", qos=1, pid=0x1234)
        _fb, body = hb.MqttStreamParser().feed(pkt)[0]
        self.assertEqual(hb.parse_publish(pkt[0], body), ("invmonitor", 1, 0x1234, b"data"))

    def test_non_ascii_topic_is_decoded_leniently(self):
        body = struct.pack(">H", 2) + b"\xff\xfe" + b"p"
        topic, _q, _pid, payload = hb.parse_publish(0x30, body)
        self.assertEqual(payload, b"p")
        self.assertIsInstance(topic, str)


class ConnectParsingTests(unittest.TestCase):
    @staticmethod
    def _body(flags, extra=b"", client_id="COMWF"):
        return (hb.enc_str("MQTT") + bytes([4, flags]) + struct.pack(">H", 60)
                + hb.enc_str(client_id) + extra)

    def test_without_credentials(self):
        self.assertEqual(hb.parse_connect(self._body(0x02)), ("COMWF", None))

    def test_with_username_and_password_never_returns_the_password(self):
        body = self._body(0xC2, hb.enc_str("user") + hb.enc_str("s3cret"))
        result = hb.parse_connect(body)
        self.assertEqual(result, ("COMWF", "user"))
        self.assertNotIn("s3cret", repr(result))

    def test_will_fields_are_skipped_before_the_username(self):
        extra = hb.enc_str("will/topic") + hb.enc_str("gone") + hb.enc_str("user")
        self.assertEqual(hb.parse_connect(self._body(0x86, extra)), ("COMWF", "user"))


if __name__ == "__main__":
    unittest.main()
