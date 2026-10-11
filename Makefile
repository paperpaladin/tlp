# The three-valued logic the whole project rests on, checked against
# SQLite, and the conformance check itself checked by breaking it.
test:
	@python3 t/test_tlp.py

# Exhaustive, and slower at higher depths.
truth:
	@python3 tlp.py truth --depth $(DEPTH)
DEPTH = 2

.PHONY: test truth
