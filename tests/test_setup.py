import runpy
from pathlib import Path
import tempfile
import unittest


setup = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "setup.py"))
record_invitation = setup["record_invitation"]


class InvitationTests(unittest.TestCase):
    def test_existing_code_requires_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            invitation = Path(directory) / ".pikpak" / "affiliate"
            record_invitation(invitation, "001234", overwrite=False)

            record_invitation(invitation, "012329", overwrite=False)
            self.assertEqual(invitation.read_text(encoding="utf-8"), "001234")

            record_invitation(invitation, "012329", overwrite=True)
            self.assertEqual(invitation.read_text(encoding="utf-8"), "012329")

    def test_blank_record_does_not_block_installation_code(self):
        with tempfile.TemporaryDirectory() as directory:
            invitation = Path(directory) / "affiliate"
            invitation.write_text(" \n", encoding="utf-8")

            record_invitation(invitation, "012329", overwrite=False)
            self.assertEqual(invitation.read_text(encoding="utf-8"), "012329")


if __name__ == "__main__":
    unittest.main()
