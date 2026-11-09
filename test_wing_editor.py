import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from channels import ChannelConfig
from wing_editor import MainWindow


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.scan_patch = patch("wing_editor.WingWorker.scan_and_connect")
        self.scan_patch.start()
        self.window = MainWindow()

    def tearDown(self):
        self.window.worker.loop.call_soon_threadsafe(self.window.worker.loop.stop)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.scan_patch.stop()

    def test_initial_count_and_offline_editing(self):
        self.assertEqual(self.window.input_count.text(), "Available inputs: 24/24")
        self.assertTrue(self.window.grid_widget.isEnabled())
        self.assertFalse(self.window.apply_btn.isEnabled())
        self.assertTrue(all(not row[4].isChecked()
                            for row in self.window.rows.values()))

    def test_name_entry_limits_typing_and_paste_to_twelve_characters(self):
        name = self.window.rows[1][0]
        self.assertEqual(name.maxLength(), 12)
        name.setText("123456789012")
        name.insert("3")
        self.assertEqual(name.text(), "123456789012")
        name.clear()
        self.app.clipboard().setText("1234567890123456")
        name.paste()
        self.assertEqual(name.text(), "123456789012")
        self.assertEqual(self.window.get_config()[1].name, "123456789012")

    def test_phantom_toggle_collected_and_reset(self):
        self.window.rows[1][0].setText("Mic")
        self.window.rows[1][4].setChecked(True)
        self.assertTrue(self.window.get_config()[1].phantom)
        self.window.set_config({1: ChannelConfig("Mic")})
        self.assertFalse(self.window.rows[1][4].isChecked())
        self.window.set_config({2: ChannelConfig("Vox", phantom=True)})
        self.assertFalse(self.window.rows[1][4].isChecked())
        self.assertTrue(self.window.rows[2][4].isChecked())

    def test_live_count_mode_and_alignment(self):
        self.window.rows[1][0].setText("Mic")
        self.window.rows[2][0].setText("Keys")
        self.window.rows[2][3].setCurrentText("Stereo")
        self.assertEqual(self.window.input_count.text(), "Available inputs: 20/24")
        self.assertEqual(self.window.input_labels[2].text(), "3 + 4")
        self.window.rows[1][0].clear()
        self.assertEqual(self.window.input_count.text(), "Available inputs: 22/24")
        self.assertEqual(self.window.input_labels[2].text(), "1 + 2")

    def test_move_keeps_all_properties_and_reallocates(self):
        mono = ChannelConfig("Mic", "Red", "Microphone")
        stereo = ChannelConfig("Keys", "Coral", "Organ", "stereo", True)
        self.window.set_config({1: mono, 2: stereo})
        self.window.move_channel(2, -1)
        self.assertEqual(self.window.get_config(), {1: stereo, 2: mono})
        self.assertEqual(self.window.input_labels[1].text(), "1 + 2")
        self.assertEqual(self.window.input_labels[2].text(), "3")
        self.window.move_channel(2, 1)
        self.assertEqual(self.window.get_config(), {1: stereo, 3: mono})

    def test_exact_capacity_and_overflow_apply_guard(self):
        self.window.conn_info = "Test console"
        self.window.set_config({
            ch: ChannelConfig(str(ch), mode="stereo") for ch in range(1, 13)})
        self.assertEqual(self.window.input_count.text(), "Available inputs: 0/24")
        self.assertTrue(self.window.apply_btn.isEnabled())
        self.window.rows[13][0].setText("Overflow")
        self.assertIn("over by 1", self.window.input_count.text())
        self.assertFalse(self.window.apply_btn.isEnabled())
        self.window.rows[13][0].clear()
        self.assertTrue(self.window.apply_btn.isEnabled())

    def test_invalid_load_preserves_existing_grid(self):
        self.window.set_config({1: ChannelConfig("Mic")})
        with self.assertRaises(ValueError):
            self.window.set_config({25: ChannelConfig("Invalid")})
        self.assertEqual(self.window.get_config(), {1: ChannelConfig("Mic")})

    def test_read_and_apply_lock_editing_until_complete(self):
        self.window.conn_info = "Test console"
        self.window.set_busy(True)
        self.assertFalse(self.window.grid_widget.isEnabled())
        self.assertFalse(self.window.load_btn.isEnabled())
        self.assertFalse(self.window.rescan_btn.isEnabled())
        self.assertFalse(self.window.apply_btn.isEnabled())
        self.window.on_apply_failed("Test failure")
        self.assertTrue(self.window.grid_widget.isEnabled())
        self.assertTrue(self.window.apply_btn.isEnabled())

    def test_apply_requires_confirmation_and_passes_current_setup(self):
        config = {1: ChannelConfig("Keys", mode="stereo")}
        self.window.set_config(config)
        with patch("wing_editor.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.No), \
                patch.object(self.window.worker, "apply") as apply:
            self.window.apply_to_wing()
            apply.assert_not_called()
        with patch("wing_editor.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.window.worker, "apply") as apply:
            self.window.apply_to_wing()
            apply.assert_called_once_with(config)
        self.assertTrue(self.window.busy)

    def test_clearing_last_name_allows_applying_empty_setup(self):
        self.window.set_config({1: ChannelConfig("Mic")})
        self.window.conn_info = "Test console"
        self.window.rows[1][0].clear()
        self.assertTrue(self.window.apply_btn.isEnabled())
        with patch("wing_editor.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.Yes) as confirm, \
                patch.object(self.window.worker, "apply") as apply:
            self.window.apply_to_wing()
            apply.assert_called_once_with({})
            self.assertIn("unused local inputs", confirm.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
