# Lessons

One line per entry: the date, the mistake, and the rule that prevents it.

- 2026-11-03: a zero count was drawn as a red pill; the owner wants no pill at all when nothing is left. Rule: an empty count is absence, not an alarm; show nothing rather than a zero.
- 2026-11-11: a mode that must keep a background engine from acting was first enforced only where the menu is redrawn; review found the engine could still switch during the handoff. Rule: put a "may act" check in the actor itself, at the last point before the write and under its locks, and let one function both read and record the mode. Trial record (implementer at high effort): 4 local review rounds, round 1 had 2 P1 and 1 P2, round 4 clean.
