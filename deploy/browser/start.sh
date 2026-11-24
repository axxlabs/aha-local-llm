#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Axxlabs Limited
# The browser container's start (SPEC §3.4 "Browser profiles"): mount the encrypted profiles here, in this
# container only, then start the supervisor (one browser per person) in a virtual display.
set -eu
PASS=/run/aha/profiles-key
if [ ! -f /profiles.enc/gocryptfs.conf ]; then
  gocryptfs -init -q -passfile "$PASS" /profiles.enc  # the first start: an empty, encrypted folder
fi
mkdir -p /profiles
gocryptfs -q -noprealloc -passfile "$PASS" /profiles.enc /profiles
exec xvfb-run -a -s "-screen 0 1920x1080x24 -nolisten tcp" node /etc/aha-browser/supervisor.mjs
