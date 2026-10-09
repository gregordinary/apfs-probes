#!/usr/bin/env bash
# names.sh over planes 1 to 3, plane 14's assigned block, and a sample of
# every other plane.
SWEEP_PART=rest exec bash "$(dirname "$0")/names.sh" "$@"
