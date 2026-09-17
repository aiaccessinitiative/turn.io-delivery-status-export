#!/usr/bin/env python3
"""Phone-number-storing entry point for the puller. NOT anonymised.

This is turn_export_anon.py with --phone-key forced on, under a name that says
what it does. Same code path, same flags, same resumability. Differences:

  * no dissemination roster is needed or read
  * the key column is `phone` (the recipient's number), not `ppbno`
  * output is data/farmers_delivery_status_with_phone_<stamp>.csv

Everything it writes contains phone numbers. Keep the outputs out of the repo
and shared drives, and delete them after handover.

  python turn_export_with_phone.py --from 2026-06-05T00:00:00.000Z --until 2026-06-06T00:00:00.000Z --auto-band
"""
import sys

import turn_export_anon

if __name__ == "__main__":
    if "--phone-key" not in sys.argv:
        sys.argv.append("--phone-key")
    turn_export_anon.main()
