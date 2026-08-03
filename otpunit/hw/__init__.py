"""Hardware back-ends, behind protocols so the UI never imports a driver.

Each module offers a real implementation (I2C OLED, GPIO buttons), a fake
for tests, and a terminal implementation for `python3 -m otpunit --sim`.
"""
