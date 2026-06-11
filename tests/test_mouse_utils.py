import subprocess
import unittest
from unittest.mock import patch

from wechat_ai_bot.utils import mouse


class MouseUtilsTest(unittest.TestCase):
    def test_press_maps_enter_to_return_keysym(self):
        with patch("wechat_ai_bot.utils.mouse.subprocess.run") as run:
            mouse.press("enter")

        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["xdotool", "key", "Return"])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_hotkey_maps_common_key_aliases(self):
        with patch("wechat_ai_bot.utils.mouse.subprocess.run") as run:
            mouse.hotkey("ctrl", "a")

        self.assertEqual(run.call_args.args[0], ["xdotool", "key", "ctrl+a"])

    def test_press_raises_when_xdotool_rejects_key(self):
        with patch(
            "wechat_ai_bot.utils.mouse.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["xdotool", "key", "bad"]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                mouse.press("bad")


if __name__ == "__main__":
    unittest.main()
