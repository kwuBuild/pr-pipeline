#!/bin/sh
# The repository's single verification command.
set -e
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -v
