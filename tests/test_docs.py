"""Generated files (example configuration, key and command references) match the code."""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestGeneratedDocs(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 13), "the help layout differs before Python 3.13; checked on 3.13+")
    def test_generated_files_are_up_to_date(self):
        tool = os.path.join(ROOT, "tools", "gen_docs.py")
        if not os.path.exists(tool):  # installed copy: no tools/
            self.skipTest("tools/ not shipped")
        res = subprocess.run([sys.executable, tool, "--check"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
