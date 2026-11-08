"""Decoding of Hypon `invmonitor` / `invinfo` payloads and the sensor table."""
import unittest

import support
from support import SN, hb


class DecodeMonitorTests(unittest.TestCase):
    def test_all_fields_and_scaling(self):
        payload = support.build_monitor(
            power=412, pv1=(31.5, 6.2), pv2=(29.9, 5.0), grid_v=231.4, grid_a=1.8, freq=49.98,
            temp=41.3, e_total=933.99, e_pv1=500.25, e_pv2=433.74, device_time="2026-11-08 12:34:56")
        data = hb.decode_monitor(payload)
        self.assertEqual(data["device_time"], "2026-11-08 12:34:56")
        self.assertEqual(data["collector_sn"], support.COLLECTOR_SN)
        self.assertEqual(data["inverter_sn"], SN)
        self.assertEqual(data["power"], 412)
        self.assertEqual((data["pv1_voltage"], data["pv1_current"]), (31.5, 6.2))
        self.assertEqual((data["pv2_voltage"], data["pv2_current"]), (29.9, 5.0))
        self.assertEqual(data["pv1_power"], round(31.5 * 6.2, 1))
        self.assertEqual(data["pv2_power"], round(29.9 * 5.0, 1))
        self.assertEqual(data["grid_voltage"], 231.4)
        self.assertEqual(data["grid_current"], 1.8)
        self.assertEqual(data["grid_frequency"], 49.98)
        self.assertEqual(data["temperature"], 41.3)
        self.assertAlmostEqual(data["energy_total"], 933.99, places=2)
        self.assertAlmostEqual(data["energy_pv1"], 500.25, places=2)
        self.assertAlmostEqual(data["energy_pv2"], 433.74, places=2)

    def test_negative_temperature_is_signed(self):
        data = hb.decode_monitor(support.build_monitor(temp=-3.5))
        self.assertEqual(data["temperature"], -3.5)

    def test_night_frame_is_all_zero_production(self):
        data = hb.decode_monitor(support.build_monitor(e_total=10.0))
        for key in hb.ZERO_WHEN_OFFLINE:
            self.assertEqual(data[key], 0, key)

    def test_too_short_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            hb.decode_monitor(support.build_monitor()[:-1])
        with self.assertRaises(ValueError):
            hb.decode_monitor(b"")

    def test_longer_payload_is_accepted(self):
        # firmware revisions may append fields: extra trailing bytes must not break decoding
        data = hb.decode_monitor(support.build_monitor(power=7) + b"\x00" * 32)
        self.assertEqual(data["power"], 7)

    def test_json_serialisable(self):
        import json
        json.dumps(hb.decode_monitor(support.build_monitor(power=1)))


class DecodeInvinfoTests(unittest.TestCase):
    def test_extracts_serial_firmware_and_model(self):
        info = hb.decode_invinfo(support.build_invinfo())
        self.assertEqual(info, {"inverter_sn": SN, "firmware": "H123-45678-90", "model": "HMS-800W-2T"})

    def test_missing_firmware_and_model_are_none(self):
        info = hb.decode_invinfo(b"X" * 20 + SN.encode() + b" nothing useful ")
        self.assertEqual(info["inverter_sn"], SN)
        self.assertIsNone(info["firmware"])
        self.assertIsNone(info["model"])


class SensorTableTests(unittest.TestCase):
    def test_every_sensor_key_exists_in_decoded_data(self):
        data = hb.decode_monitor(support.build_monitor())
        for key, *_ in hb.SENSORS:
            self.assertIn(key, data)

    def test_sensor_keys_are_unique(self):
        keys = [s[0] for s in hb.SENSORS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_energy_counters_are_total_increasing(self):
        for key, _name, unit, dev_class, state_class in hb.SENSORS:
            if dev_class == "energy":
                self.assertEqual((unit, state_class), ("kWh", "total_increasing"), key)
            else:
                self.assertEqual(state_class, "measurement", key)

    def test_zeroed_keys_are_instantaneous_measurements_only(self):
        by_key = {s[0]: s for s in hb.SENSORS}
        for key in hb.ZERO_WHEN_OFFLINE:
            self.assertEqual(by_key[key][4], "measurement", key)  # never a total


if __name__ == "__main__":
    unittest.main()
