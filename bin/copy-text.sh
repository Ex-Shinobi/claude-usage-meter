#!/bin/sh
# Put the first argument on the clipboard. SwiftBar menu items can run a
# command but cannot pipe, so `pbcopy` needs this one-liner around it.
printf '%s' "${1:-}" | /usr/bin/pbcopy
