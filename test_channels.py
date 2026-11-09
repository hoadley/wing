import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from channels import (
    ChannelConfig, allocate_inputs, apply_channel_aesthetics, load_config,
    read_channel_aesthetics, save_config, validate_capacity,
)


class FakeWing:
    def __init__(self):
        self.values = {}
        self.sent = []

    def send(self, address, value):
        self.sent.append((address, value))
        self.values[address] = value

    async def get(self, address):
        if address.endswith("/$name"):
            ch = address.split("/")[2]
            idx = self.values[f"/ch/{ch}/in/conn/in"]
            return self.values[f"/io/in/LCL/{idx}/name"]
        value = self.values[address]
        if isinstance(value, int):
            return [str(value), 0.0, value]
        return value


class AllocationTests(unittest.TestCase):
    def test_mono(self):
        config = {ch: ChannelConfig(f"Ch {ch}") for ch in range(1, 25)}
        self.assertEqual(validate_capacity(config),
                         {ch: (ch,) for ch in range(1, 25)})

    def test_stereo_alignment_and_sparse_strips(self):
        config = {1: ChannelConfig("Mic"),
                  3: ChannelConfig("Keys", mode="stereo"),
                  8: ChannelConfig("Vocal")}
        self.assertEqual(allocate_inputs(config),
                         {1: (1,), 3: (3, 4), 8: (5,)})

    def test_twelve_stereo_sources_exhaust_inputs(self):
        config = {ch: ChannelConfig(str(ch), mode="stereo")
                  for ch in range(1, 13)}
        self.assertEqual(validate_capacity(config)[12], (23, 24))
        config[13] = ChannelConfig("Overflow")
        with self.assertRaisesRegex(ValueError, "25 local input slots"):
            validate_capacity(config)

    def test_alignment_gaps_count_against_capacity(self):
        config = {ch: ChannelConfig(str(ch)) for ch in range(1, 24)}
        config[24] = ChannelConfig("Stereo", mode="stereo")
        self.assertEqual(allocate_inputs(config)[24], (25, 26))
        with self.assertRaisesRegex(ValueError, "26 local input slots"):
            validate_capacity(config)

    def test_reordering_can_recover_alignment_gap(self):
        mono, stereo = ChannelConfig("Mic"), ChannelConfig("Keys", mode="stereo")
        self.assertEqual(allocate_inputs({1: mono, 2: stereo})[2], (3, 4))
        self.assertEqual(allocate_inputs({1: stereo, 2: mono})[2], (3,))

    def test_invalid_entries(self):
        for ch, entry in [
                (0, ChannelConfig("Mic")), (25, ChannelConfig("Mic")),
                (1, ChannelConfig("")), (1, ChannelConfig("x" * 13)),
                (1, ChannelConfig("Mic", mode="surround")),
                (1, ChannelConfig("Mic", color="invalid")),
                (1, ChannelConfig("Mic", icon="invalid"))]:
            with self.subTest(ch=ch, entry=entry), self.assertRaises(ValueError):
                allocate_inputs({ch: entry})

    def test_twelve_character_name_is_valid(self):
        self.assertEqual(
            validate_capacity({1: ChannelConfig("x" * 12)}), {1: (1,)})


class ConfigTests(unittest.TestCase):
    def test_legacy_config_defaults_to_mono(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps({
                "1": {"name": "Mic", "color": "Red", "icon": "Microphone"},
                "2": {"name": "Vox"},
            }))
            config = load_config(str(path))
        self.assertEqual(len(config), 2)
        self.assertTrue(all(entry.mode == "mono" for entry in config.values()))
        self.assertTrue(all(entry.phantom is False for entry in config.values()))

    def test_round_trip(self):
        config = {5: ChannelConfig("Keys", "Coral", "Organ", "stereo", True),
                  1: ChannelConfig("Vox", "Red", "Microphone")}
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "setup.json")
            save_config(config, path)
            self.assertEqual(load_config(path), config)
            self.assertEqual(list(json.loads(Path(path).read_text())), ["1", "5"])

    def test_overflow_does_not_overwrite_file(self):
        config = {ch: ChannelConfig(str(ch), mode="stereo")
                  for ch in range(1, 14)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            path.write_text("unchanged")
            with self.assertRaises(ValueError):
                save_config(config, str(path))
            self.assertEqual(path.read_text(), "unchanged")

    def test_invalid_files(self):
        for data in ([], {"1": []}, {"25": {"name": "Mic"}},
                     {"1": {"name": "Mic", "mode": "bad"}},
                     {"1": {"name": "x" * 13}},
                     {"1": {"name": "Mic", "phantom": "false"}},
                     {"1": {"name": "Mic", "phantom": 1}},
                     {"1": {"name": "Mic"}, "01": {"name": "Duplicate"}}):
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "setup.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_config(str(path))


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_removing_channel_clears_old_name_after_reallocation(self):
        wing = FakeWing()
        await apply_channel_aesthetics(wing, {
            1: ChannelConfig("Mic"), 2: ChannelConfig("Keys"),
            3: ChannelConfig("Vox")})
        config = {1: ChannelConfig("Mic"), 3: ChannelConfig("Vox")}
        await apply_channel_aesthetics(wing, config)
        self.assertEqual(wing.values["/ch/2/in/conn/grp"], "OFF")
        self.assertEqual(wing.values["/io/in/LCL/2/name"], "Vox")
        self.assertEqual(wing.values["/io/in/LCL/3/name"], "")
        self.assertEqual(await read_channel_aesthetics(wing), config)

    async def test_unused_names_cleared_without_changing_other_input_settings(self):
        wing = FakeWing()
        for idx in range(1, 25):
            wing.values[f"/io/in/LCL/{idx}/name"] = f"Old {idx}"
            wing.values[f"/io/in/LCL/{idx}/vph"] = 1
        wing.values["/io/in/LCL/25/name"] = "Out of scope"
        await apply_channel_aesthetics(wing, {
            1: ChannelConfig("Mic"),
            2: ChannelConfig("Pair", mode="stereo")})
        for idx in (2, *range(5, 25)):
            self.assertEqual(wing.values[f"/io/in/LCL/{idx}/name"], "")
            self.assertEqual(wing.values[f"/io/in/LCL/{idx}/vph"], 1)
            self.assertEqual(
                [(address, value) for address, value in wing.sent
                 if address.startswith(f"/io/in/LCL/{idx}/")],
                [(f"/io/in/LCL/{idx}/name", "")])
        self.assertEqual(wing.values["/io/in/LCL/1/name"], "Mic")
        self.assertEqual(wing.values["/io/in/LCL/3/name"], "Pair")
        self.assertNotIn(("/io/in/LCL/4/name", ""), wing.sent)
        self.assertEqual(wing.values["/io/in/LCL/25/name"], "Out of scope")

    async def test_empty_setup_disconnects_all_strips_and_clears_all_local_names(self):
        wing = FakeWing()
        await apply_channel_aesthetics(
            wing, {1: ChannelConfig("Last input", mode="stereo")})
        await apply_channel_aesthetics(wing, {})
        for idx in range(1, 25):
            self.assertEqual(wing.values[f"/io/in/LCL/{idx}/name"], "")
            self.assertEqual(wing.values[f"/ch/{idx}/in/conn/grp"], "OFF")
        self.assertEqual(await read_channel_aesthetics(wing), {})

    async def test_unused_name_clear_failure_is_reported(self):
        class StaleNameWing(FakeWing):
            async def get(self, address):
                if address == "/io/in/LCL/24/name":
                    return "Stale name"
                return await super().get(address)

        with self.assertRaisesRegex(RuntimeError, "LCL 24: could not clear"):
            await apply_channel_aesthetics(StaleNameWing(), {})

    async def test_phantom_applies_to_mono_and_both_stereo_inputs(self):
        wing = FakeWing()
        config = {
            1: ChannelConfig("Mic", phantom=True),
            2: ChannelConfig("Stereo", mode="stereo", phantom=True),
            3: ChannelConfig("Vox"),
        }
        await apply_channel_aesthetics(wing, config)
        for idx, state in ((1, 1), (3, 1), (4, 1), (5, 0)):
            self.assertEqual(wing.values[f"/io/in/LCL/{idx}/vph"], state)
        self.assertNotIn("/io/in/LCL/2/vph", wing.values)
        self.assertEqual(await read_channel_aesthetics(wing), config)
        config[1] = ChannelConfig("Mic")
        config[2] = ChannelConfig("Stereo", mode="stereo")
        await apply_channel_aesthetics(wing, config)
        for idx in (1, 3, 4, 5):
            self.assertEqual(wing.values[f"/io/in/LCL/{idx}/vph"], 0)

    async def test_phantom_mismatch_is_reported(self):
        class WrongPhantomWing(FakeWing):
            async def get(self, address):
                if address == "/io/in/LCL/2/vph":
                    return "0"
                return await super().get(address)

        wing = WrongPhantomWing()
        results = await apply_channel_aesthetics(
            wing, {1: ChannelConfig("Pair", mode="stereo", phantom=True)})
        self.assertIn("/io/in/LCL/2/vph", results[0][1])
        self.assertIn("MISMATCH", results[0][1])
        with self.assertRaisesRegex(ValueError, "different 48V states"):
            await read_channel_aesthetics(wing)

    async def test_mono_moves_free_pairs_before_any_stereo_changes(self):
        class OccupiedWing(FakeWing):
            def send(self, address, value):
                if value == "ST" and address.endswith("/mode"):
                    self.assert_pair_free(address)
                super().send(address, value)

            def assert_pair_free(self, address):
                first = int(address.split("/")[-2])
                for ch in (2, 4):
                    idx = self.values[f"/ch/{ch}/in/conn/in"]
                    if idx in (first, first + 1):
                        raise AssertionError(f"Mono channel {ch} still occupies pair")

        wing = OccupiedWing()
        wing.values.update({
            "/ch/2/in/conn/in": 1,
            "/ch/4/in/conn/in": 5,
        })
        config = {1: ChannelConfig("Keys", mode="stereo"),
                  2: ChannelConfig("Mic"),
                  3: ChannelConfig("Organ", mode="stereo"),
                  4: ChannelConfig("Vox")}
        results = await apply_channel_aesthetics(wing, config)
        first_stereo_change = wing.sent.index(("/io/in/LCL/1/mode", "M"))
        for ch, idx in ((2, 3), (4, 7)):
            self.assertLess(
                wing.sent.index((f"/ch/{ch}/in/conn/in", idx)),
                first_stereo_change)
        self.assertEqual(wing.values["/ch/1/in/conn/in"], 1)
        self.assertEqual(wing.values["/ch/3/in/conn/in"], 5)
        self.assertEqual([ch for ch, _ in results], [1, 2, 3, 4])
        self.assertEqual(await read_channel_aesthetics(wing), config)

    async def test_failed_mono_verification_blocks_stereo_changes(self):
        class StuckWing(FakeWing):
            async def get(self, address):
                if address == "/ch/2/in/conn/in":
                    return "1"
                return await super().get(address)

        wing = StuckWing()
        with self.assertRaisesRegex(RuntimeError, "stereo changes were not applied"):
            await apply_channel_aesthetics(wing, {
                1: ChannelConfig("Keys", mode="stereo"),
                2: ChannelConfig("Mic")})
        self.assertFalse(any(address.startswith("/io/in/LCL/1/")
                             for address, _ in wing.sent))
        self.assertNotIn(("/ch/1/in/conn/in", 1), wing.sent)

    async def test_apply_routes_modes_and_identities_and_reads_back(self):
        wing = FakeWing()
        config = {1: ChannelConfig("Mic", "Red", "Microphone"),
                  2: ChannelConfig("Keys", "Coral", "Organ", "stereo"),
                  3: ChannelConfig("Vox", "Green", "Male Vocal")}
        results = await apply_channel_aesthetics(wing, config)
        self.assertTrue(all(status.endswith("ok") for _, status in results))
        self.assertEqual(wing.values["/ch/2/in/conn/in"], 3)
        self.assertEqual(wing.values["/io/in/LCL/3/mode"], "ST")
        self.assertEqual(wing.values["/ch/3/in/conn/in"], 5)
        self.assertEqual(wing.values["/io/in/LCL/5/mode"], "M")
        self.assertEqual(wing.values["/ch/24/in/conn/grp"], "OFF")
        self.assertEqual(await read_channel_aesthetics(wing), config)

    async def test_invalid_capacity_sends_nothing(self):
        wing = FakeWing()
        config = {ch: ChannelConfig(str(ch), mode="stereo")
                  for ch in range(1, 14)}
        with self.assertRaises(ValueError):
            await apply_channel_aesthetics(wing, config)
        self.assertEqual(wing.sent, [])

    async def test_apply_reports_routing_and_mode_mismatch(self):
        class WrongWing(FakeWing):
            async def get(self, address):
                if address == "/ch/1/in/conn/in":
                    return "9"
                if address == "/io/in/LCL/1/mode":
                    return "ST"
                return await super().get(address)

        results = await apply_channel_aesthetics(
            WrongWing(), {1: ChannelConfig("Mic")})
        self.assertIn("MISMATCH", results[0][1])
        self.assertIn("/ch/1/in/conn/in", results[0][1])
        self.assertIn("/io/in/LCL/1/mode", results[0][1])

    async def test_read_rejects_external_bank(self):
        wing = FakeWing()
        wing.values["/ch/1/in/conn/grp"] = "A"
        with self.assertRaisesRegex(ValueError, "not LCL"):
            await read_channel_aesthetics(wing, [1])

    async def test_read_propagates_timeout(self):
        class TimeoutWing(FakeWing):
            async def get(self, address):
                raise asyncio.TimeoutError("No console reply")

        with self.assertRaises(asyncio.TimeoutError):
            await read_channel_aesthetics(TimeoutWing(), [1])

    async def test_stereo_to_mono_resets_source_mode(self):
        wing = FakeWing()
        await apply_channel_aesthetics(
            wing, {1: ChannelConfig("Keys", mode="stereo")})
        await apply_channel_aesthetics(
            wing, {1: ChannelConfig("Mic"), 2: ChannelConfig("Vox")})
        self.assertEqual(wing.values["/io/in/LCL/1/mode"], "M")
        self.assertEqual(wing.values["/io/in/LCL/2/mode"], "M")


if __name__ == "__main__":
    unittest.main()
