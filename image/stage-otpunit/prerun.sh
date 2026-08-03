#!/bin/bash -e
# pi-gen copies the previous stage's rootfs forward before this stage runs.

if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
