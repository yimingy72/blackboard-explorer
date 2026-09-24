"""Verify all seeded findings against independently started services."""

import p1
import p2
import p3
import p4
import p5
import p6

for case in (p1, p2, p3, p4, p5, p6):
    print(case.__name__.upper(), case.verify())
