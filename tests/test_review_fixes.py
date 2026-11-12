"""Regressions for defects found by reading the code against real hardware
behaviour, none of which the fakes could show."""
import os
import pty
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from otpunit import config

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "device" / "install.sh"
SERVICE = REPO / "device" / "systemd" / "otp-unit.service"


def test_arrow_keys_and_fast_typing_survive_a_real_tty():
    """Buffered stdin swallowed the tail of every escape sequence."""
    master, slave = pty.openpty()
    script = textwrap.dedent("""
        import sys
        from otpunit.hw.buttons import KeyboardButtons
        kb = KeyboardButtons()
        print("READY", flush=True, file=sys.stderr)
        got = []
        while len(got) < 5:
            press = kb.wait(timeout=5)
            if press is None:
                break
            got.append(press.name)
        kb.close()
        print(" ".join(got), file=sys.stderr)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave,
                            stdout=slave, stderr=subprocess.PIPE, cwd=REPO,
                            env=dict(os.environ, PYTHONPATH=str(REPO)))
    os.close(slave)
    assert proc.stderr.readline().strip() == b"READY"
    os.write(master, b"\x1b[A\x1b[B\rud")
    out = proc.stderr.read().decode().strip()
    proc.wait(timeout=20)
    os.close(master)
    assert out == "UP DOWN OK UP DOWN", out


@pytest.mark.parametrize("word, ok", [
    ("RUSTED-BADGER", True),
    ("ÆBLE-ØRN", False),          # not in the page font
    ("A" * 40, False),            # would overprint the AUTH group
])
def test_a_hand_edited_auto_codeword_must_be_printable(word, ok):
    assert (not config.Settings(auto_codeword=word).validate()) is ok


def _set_config(tmp_path, before: str) -> str:
    text = INSTALL.read_text()
    start = text.index("config_applies_to_all() {")
    end = text.index("\n}\n", text.index("set_config() {")) + 3
    cfg = tmp_path / "config.txt"
    cfg.write_text(before)
    script = (f'CONFIG_TXT="{cfg}"\n{text[start:end]}\n'
              'set_config "dtparam=i2c_arm=on"\nset_config "dtparam=i2c_arm=on"\n')
    subprocess.run(["bash", "-euc", script], check=True)
    return cfg.read_text()


def test_config_txt_settings_are_never_left_inside_a_model_section(tmp_path):
    out = _set_config(tmp_path, "a=1\n[pi4]\ndtparam=i2c_arm=on\n")
    assert out.endswith("[all]\ndtparam=i2c_arm=on\n"), out


def test_config_txt_setting_is_added_once(tmp_path):
    out = _set_config(tmp_path, "[cm5]\nx=1\n[all]\n")
    assert out.count("dtparam=i2c_arm=on") == 1 and "[all]\ndtparam" in out
    assert _set_config(tmp_path, "dtparam=i2c_arm=on\n") == "dtparam=i2c_arm=on\n"


def test_the_service_cannot_start_with_swap_on_or_a_read_only_cwd():
    unit = SERVICE.read_text()
    assert "ExecStartPre=-/sbin/swapoff -a" in unit
    # lgpio writes a FIFO into the cwd at import; /opt is read-only here.
    assert "WorkingDirectory=/run/otp-unit" in unit
    assert "RuntimeDirectory=otp-unit" in unit
    assert "systemctl mask swap.target" in INSTALL.read_text()
