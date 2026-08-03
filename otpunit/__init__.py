"""Headless OTP pad print unit for the Raspberry Pi.

Boots into a single-purpose appliance: plug in a USB laser printer, pick a
codeword and a page count on a small OLED with three buttons, and it prints
the A and B copies of a pad pair back to back, then wipes itself.

Key material is generated into RAM and piped to the printer; no pad ever
becomes a file. See otpunit.jobs for the pad-pair pipeline and
otpunit.printer for the one caveat to that claim.
"""

__all__ = ["codewords", "config", "jobs", "printer", "ui"]
