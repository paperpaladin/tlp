#!/usr/bin/env python3
"""tlp -- find queries where SQLite returns the wrong rows.

Not crashes. Wrong answers: a query whose result differs from what the data
says it should be, with no error and nothing to compare against. That class
is invisible to ordinary testing, because there is no second implementation
to disagree with and no exception to catch.

The way out is a *metamorphic* oracle -- a relation the engine must satisfy
with ITSELF, so no reference implementation is needed at all.

TLP, ternary logic partitioning (Rigger and Su, OOPSLA 2020). SQL is
three-valued, so for any predicate p every row of t makes p exactly one of
TRUE, FALSE or NULL. Therefore:

    SELECT * FROM t WHERE p
      UNION ALL SELECT * FROM t WHERE NOT p
      UNION ALL SELECT * FROM t WHERE p IS NULL

must be the same multiset as SELECT * FROM t. Always. Any predicate, any
data, any engine. If it is not, the optimiser dropped or duplicated a row.

The three partitions are also why the predicate generator matters more than
it looks: a predicate that is never NULL leaves the third arm empty, and the
test degenerates into "the engine agrees with itself about TRUE and FALSE",
which it will. Measuring that is what `tlp vacuity` is for.

    tlp.py truth [--depth N]   three-valued logic, exhaustively
"""
import argparse
import itertools
import sqlite3
import sys

# --------------------------------------------------------------- 3VL model
#
# The SQL standard's truth tables, transcribed. This is the one place in the
# project where the expected answer is written down by hand rather than
# taken from an authority, so it is worth being precise about what that
# buys: it is a CALIBRATION, not the oracle. The fuzzer's oracle is the
# metamorphic relation below, which needs no model at all. This exists to
# prove the model of three-valued logic the generator is built on matches
# the engine, before anything is built on it.
#
# Python's True/False/None stand for SQL's TRUE/FALSE/NULL.

TRI = (True, False, None)


def t_not(a):
    return None if a is None else not a


def t_and(a, b):
    # FALSE dominates: FALSE AND NULL is FALSE, not NULL, because no value
    # of the unknown operand could make the conjunction true.
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def t_or(a, b):
    # TRUE dominates, by the mirror argument.
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def t_is(a, b):
    # IS compares the three values themselves, so it is total: it never
    # yields NULL. This is the operator that lets a query ask about NULL
    # rather than propagate it.
    return a is b


def t_eq(a, b):
    # = propagates. NULL = NULL is NULL, which is the single fact most
    # often got wrong by people and by optimisers alike.
    if a is None or b is None:
        return None
    return a == b


# Unary postfix tests, all total.
UNARY_TESTS = {
    "IS NULL": lambda a: a is None,
    "IS NOT NULL": lambda a: a is not None,
    "IS TRUE": lambda a: a is True,
    "IS FALSE": lambda a: a is False,
    "IS NOT TRUE": lambda a: a is not True,
    "IS NOT FALSE": lambda a: a is not False,
}

BINARY_OPS = {
    "AND": t_and,
    "OR": t_or,
    "IS": t_is,
    "IS NOT": lambda a, b: not t_is(a, b),
    "=": t_eq,
    "<>": lambda a, b: None if t_eq(a, b) is None else not t_eq(a, b),
}

# How each three-valued constant is written in SQL. SQLite accepts the TRUE
# and FALSE keywords since 3.23; using them rather than 1 and 0 keeps the
# generated text readable and exercises the keyword path.
LITERAL = {True: "TRUE", False: "FALSE", None: "NULL"}


def enumerate_exprs(depth):
    """Every expression of the grammar up to `depth`, as (sql, value) pairs.

    Exhaustive rather than random, because the space is small enough to
    cover completely and a sampled truth table is not a truth table.
    """
    level = [(LITERAL[v], v) for v in TRI]
    seen = list(level)
    for _ in range(depth):
        nxt = []
        for sql, val in level:
            nxt.append(("(NOT %s)" % sql, t_not(val)))
            for name, fn in UNARY_TESTS.items():
                nxt.append(("(%s %s)" % (sql, name), fn(val)))
        for (asql, aval), (bsql, bval) in itertools.product(level, repeat=2):
            for name, fn in BINARY_OPS.items():
                nxt.append(("(%s %s %s)" % (asql, name, bsql), fn(aval, bval)))
        seen.extend(nxt)
        level = nxt
    return seen


def sql_value(db, sql):
    """What SQLite says the expression is, as a three-valued Python value."""
    (raw,) = db.execute("SELECT " + sql).fetchone()
    if raw is None:
        return None
    return bool(raw)


def cmd_truth(args):
    db = sqlite3.connect(":memory:")
    exprs = enumerate_exprs(args.depth)
    bad = 0
    for sql, want in exprs:
        got = sql_value(db, sql)
        if got is not want:
            bad += 1
            if bad <= 10:
                print("MISMATCH %s -> sqlite %r, standard %r" % (sql, got, want))
    print("%d expressions, %d disagree with the standard (depth %d)"
          % (len(exprs), bad, args.depth))
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser(prog="tlp", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("truth", help="exhaustive three-valued logic check")
    p.add_argument("--depth", type=int, default=2)
    p.set_defaults(fn=cmd_truth)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
