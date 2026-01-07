# tlp

Finds queries where SQLite returns the **wrong rows**. Not a crash, not an
error -- a result that differs from what the data says it should be, with
nothing obvious to compare against.

```
$ python3 tlp.py tlp --n 500
TLP: 500 queries, 0 disagreements, 0 skipped, 152 genuinely three-valued (30%)

$ python3 tlp.py selftest
planted bug               TLP    NoREC      agg  DISTINCT    GROUP
drop-row                 100%       0%       0%        0%      80%
duplicate-row            100%       0%       0%        0%     100%
null-only                 21%       0%       0%        0%      21%
count-off-by-one           0%     100%       0%        0%       0%
aggregate-skew             0%       0%      86%        0%       0%
distinct-dup               0%       0%       0%      100%       0%
group-skew                 0%       0%       0%        0%     100%
setop-drop-row           100%       0%       0%       80%      80%

every planted bug is caught by at least one oracle
```

## Why there is no reference implementation

Wrong-answer bugs are the hard class precisely because nothing disagrees
with them. A fuzzer that looks for crashes has an obvious oracle: the
process died. A fuzzer that looks for wrong answers needs to know what the
right answer was, and if you could compute that you would not need the
database.

The way out is a **metamorphic** oracle: a relation the engine must satisfy
with *itself*.

SQL is three-valued. For any predicate `p`, every row of `t` makes `p`
exactly one of TRUE, FALSE or NULL -- never two, never none. So the three
partitions must reassemble into the whole:

```sql
SELECT * FROM t WHERE p
  UNION ALL SELECT * FROM t WHERE NOT p
  UNION ALL SELECT * FROM t WHERE p IS NULL
```

must be the same multiset as `SELECT * FROM t`. Always. Any predicate, any
data, any engine.

That is **TLP**, ternary logic partitioning (Rigger and Su, OOPSLA 2020).
It needs no second implementation, no stored expectation, and no model of
what the query means. It cannot be satisfied by being self-consistently
wrong, which is the failure mode a hand-written expected output has.

**NoREC** (Rigger and Su, ESEC/FSE 2020) attacks from the other side. The
same predicate in a `WHERE` clause can be answered from an index; in the
select list of a subquery it cannot, because every row has to be fetched
and evaluated:

```sql
SELECT COUNT(*) FROM t WHERE p
SELECT SUM(w) FROM (SELECT ((p) IS TRUE) AS w FROM t)
```

When those disagree it is the index path that is wrong -- the half no
amount of reading the query will show you.

Between them these two techniques found over 400 real bugs in SQLite,
MySQL, PostgreSQL and TiDB.

**Aggregate TLP** is the third, and the one where the technique stops
being a one-liner. An aggregate over the whole table must equal the same
aggregate recombined from the three partitions -- but there is no single
recombination rule:

| aggregate | recombine with | why |
|---|---|---|
| `SUM` | `SUM` | sums add; `SUM` of nothing is NULL and the outer `SUM` skips it |
| `COUNT` | **`SUM`** | counts **add**, they do not count |
| `MIN` | `MIN` | `MIN` ignores NULLs at both levels, which is what makes them agree |
| `MAX` | `MAX` | the mirror of `MIN` |
| `AVG` | *(none)* | the average of three averages is not the average |

## No oracle subsumes another

That is a design claim, so it is measured rather than asserted, and the
table above is the measurement. Each oracle catches exactly the class it
was built for and **nothing else**:

- TLP never asks for a count, so it cannot see a count that is wrong.
- NoREC only ever compares two numbers, so it cannot see which rows moved.
- Both compare rows or counts, so neither can see an aggregate that is
  wrong while every row is present and every count agrees.
- None of the three writes `DISTINCT`, so a dedup step skipped on one path
  is invisible to all of them.
- None of the four groups, so one group counted wrong -- with every row
  present, every count right and every whole-table aggregate right -- is
  invisible to all of them.

### One zero in that table is not what it looks like

The `DISTINCT` column reads 0% against `drop-row`, and the tempting
reading is that the relation cannot see a lost row. It cannot be read that
way, because `drop-row` is *written* to fire on `UNION ALL` and the
`DISTINCT` relation recombines with `UNION`. The 0% is a property of the
planted bug's trigger, not of the relation.

Separating the two explanations costs one more planted bug. `setop-drop-row`
is the same lost row fired on any set operation, and the `DISTINCT` column
moves to 80% -- so the relation sees lost rows fine, and it was the bug
that could not see the relation. The last row of the table exists only to
answer that question, and without it the table would be quietly overclaiming.

### The composition rules are load-bearing

The table of rules above looks arbitrary until you use the wrong one.
Against a database that is behaving perfectly:

| aggregate | correct rule | wrong rule | wrong rule |
|---|---|---|---|
| `SUM` | 0 / 210 | `MIN` 150 | `COUNT` 208 |
| `COUNT` | 0 / 210 | `COUNT` 208 | `MIN` 210 |
| `MIN` | 0 / 210 | `SUM` 135 | `MAX` 127 |
| `MAX` | 0 / 210 | `SUM` 135 | `MIN` 125 |

Every correct rule raises **no** false alarms; every wrong one raises
hundreds. `COUNT` is the one to stare at: its combiner is `SUM`, and
writing `COUNT` there by analogy with the others -- the obvious mistake --
produces a tool that reports a bug in 208 of 210 correct queries.

## DISTINCT and GROUP BY need opposite composition rules

Each of these is the partitioning done over a query shape where the obvious
recombination is the wrong one -- the same trap as the aggregate table, and
worth meeting twice because the two traps point in opposite directions.

**`DISTINCT` recombines with `UNION`**, a set union, and it is the only
relation here that does not use `UNION ALL`. The three partitions still
cover every row, so every distinct value appears in at least one arm -- but
a value can appear in *two*, because two rows sharing a value can land on
opposite sides of the predicate:

```sql
  SELECT DISTINCT c FROM t WHERE p
    UNION SELECT DISTINCT c FROM t WHERE NOT p
    UNION SELECT DISTINCT c FROM t WHERE p IS NULL
==  SELECT DISTINCT c FROM t
```

**`GROUP BY` must recombine with `UNION ALL` inside**, for exactly the
reason `DISTINCT` must not. A group's rows are split across the three
partitions, so each arm holds part of every group, and the three partial
counts are the entire point -- a set union would collapse two arms that
happened to count the same number of rows. Then the counts are
re-aggregated per group, which is the `COUNT`-recombines-with-`SUM` rule
from the aggregate table applied group by group:

```sql
  SELECT g, SUM(n) FROM (
      SELECT g, COUNT(*) AS n FROM t WHERE p GROUP BY g
      UNION ALL SELECT g, COUNT(*) FROM t WHERE NOT p GROUP BY g
      UNION ALL SELECT g, COUNT(*) FROM t WHERE p IS NULL GROUP BY g
  ) GROUP BY g
==  SELECT g, COUNT(*) FROM t GROUP BY g
```

So each relation's rule is a false-alarm generator in the other's place,
measured against a database that is behaving correctly:

| `DISTINCT` recombined with | false alarms | | `GROUP BY` counts recombined with | false alarms |
|---|---|---|---|---|
| **`UNION`** | **0 / 200** | | **`SUM`** | **0 / 200** |
| `UNION ALL` | 113 | | `COUNT` | 196 |
| `INTERSECT` | 200 | | `MIN` | 113 |
| `EXCEPT` | 198 | | `MAX` | 113 |
| | | | `AVG` | 113 |

The `113` is the same number four times over, and it is the same event seen
from two sides: one value of the column landing in more than one partition.
`UNION ALL` then keeps it twice, and the per-arm counts then sum to more
than any single one of them. Measured directly -- a value spans partitions
in exactly 113 of those 200 cases -- rather than left as a coincidence.

### A hidden dependency on three operators agreeing about NULL

The `DISTINCT` relation quietly rests on something the SQL standard makes
easy to miss. `DISTINCT`, `UNION` and `GROUP BY` all treat two NULLs as the
*same* value, while `=` says `NULL = NULL` is NULL. If `UNION` followed the
`=` rule, a NULL arriving from two arms would survive twice and the
relation would be false for every table containing one. The suite checks
all three operators directly rather than trusting that reading of the
standard.

## Partitioning a join's ON clause

The published work found more bugs here than anywhere else, and the reason
is worth stating: a `WHERE` predicate is evaluated once per row, but an
`ON` predicate decides which **pairs** of rows exist at all. Getting it
wrong changes the shape of the result rather than its contents, and join
reordering, index selection on the inner side and the outer-join padding
rules all meet in that one clause.

Every pair in the cross product makes the predicate exactly one of TRUE,
FALSE or NULL, and an inner join keeps precisely the TRUE pairs -- so the
three `ON`-partitions are a partition of the cross product:

```sql
  t1 JOIN t2 ON p
    UNION ALL t1 JOIN t2 ON NOT p
    UNION ALL t1 JOIN t2 ON p IS NULL
==  t1, t2
```

**The same trick does not work for a `LEFT JOIN`**, which is the part worth
getting right rather than assuming. Each arm pads its own unmatched rows,
so the padding is counted three times over. What is true instead is a
decomposition -- a left join is its inner join plus one row for every left
row that found no TRUE match anywhere:

```sql
  COUNT(t1 LEFT JOIN t2 ON p)
==  COUNT(t1 JOIN t2 ON p)
  + COUNT(t1 WHERE NOT EXISTS (SELECT 1 FROM t2 WHERE p))
```

That is a different shape of query from anything else here: `NOT EXISTS`
is a correlated subquery, and the optimiser rewrites it into something else
entirely.

```
$ python3 tlp.py joinselftest
planted bug               INNER    LEFT JOIN
join-drop-pair             100%           0%
leftjoin-overpad             0%         100%

both planted join bugs are caught, and by different relations
```

Kept as a separate table from the one above rather than folded into it,
because that would not be an honest comparison: the single-table oracles
query `t` and these query `t1` and `t2`, so "TLP does not catch this" would
only mean the two were never pointed at the same thing.

Every generated `ON` predicate is forced to mention both tables. Without
that it is a filter on one side, the planner pushes it down, and the join
being tested is not the join it appears to be. 65% of them genuinely split
the cross product rather than keeping all of it or none.

## Shrinking a case to something a human will read

A found case is a schema, ten rows and a predicate like
`((c1 IS NOT NULL OR c0 <= 2) AND NOT (c2 = c0 OR c3 < 'bb'))`. Nobody
reads that, so an unreduced report is the same as no report.

The shrink is greedy delta debugging over three axes -- drop a row, replace
a predicate with one of its own subtrees, pull a value toward NULL --
keeping any change that leaves the case still failing.

**The only property that matters is soundness: what comes out must still
fail.** A reducer that shrinks past the bug emits a minimal case that
reproduces nothing and sends whoever reads it hunting for a bug that is not
there, which is strictly worse than printing the unreduced version. Every
reduction asserts it on its own output:

```
$ python3 tlp.py reduce --bug null-only
found at try 2, against the planted bug 'null-only'
  rows       10 -> 3
  predicate  13 nodes -> 1

CREATE TABLE t (c0 INTEGER, c1 TEXT, c2 INTEGER, c3 TEXT);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
INSERT INTO t VALUES (NULL, NULL, NULL, NULL);
-- predicate: c0 IS NULL
-- still fails after reduction: True
```

That output is worth reading twice. `null-only` is the narrow planted bug:
it fires only above two rows and only when the predicate itself mentions
NULL. The reducer was told neither thing, and landed on exactly three rows
and exactly `c0 IS NULL`. It rediscovered the bug's trigger condition by
shrinking, which is what a reducer is for.

Because the bugs are planted, their true floors are known, so the suite can
assert the reducer reaches them rather than merely getting smaller:

| planted bug | fires when | floor | reached |
|---|---|---|---|
| `drop-row` | partition returns > 3 rows | 4 rows | 4 |
| `duplicate-row` | any non-empty result | 1 row | 1 |
| `null-only` | > 2 rows, NULL in the predicate | 3 rows | 3 |

"Smaller than before" would be satisfied by a reducer that drops one row
and gives up. Hitting the floor is a measurement.

### The shrink order has to be well-founded

The value axis looks like the easy one and is the only part that went
wrong. The first version offered every value NULL and 0 as candidates,
counted any accepted swap as progress, and **never terminated**: NULL
became 0, then 0 became NULL, forever, each step looking locally like an
improvement.

So candidates are now strictly below the current value on a total order --
NULL, then 0 or the empty string, then everything else -- and the suite
asserts no candidate leads back to the value it replaced. Deleting the
strictness makes the test run hang rather than fail, which is the honest
way for that mutation to show up.

## The generator decides whether any of this means anything

TLP splits rows three ways. A predicate that is never NULL leaves the third
arm empty, and the relation degenerates into *"the engine agrees with
itself about TRUE and FALSE"* -- which it will, forever, while printing a
large and encouraging number.

So the fraction of predicates that are genuinely three-valued is measured,
not assumed, and the suite holds it above a floor:

```
$ python3 tlp.py vacuity --n 600
600 predicates over 8-row tables, NULL rate 0.25
   78.0%  have a non-empty NULL partition
   31.5%  are genuinely three-valued (all three non-empty)
   12.3%  are constant across every row (partition nothing)
```

Two generator decisions do all the work, and both are about making
comparisons **collide**. The value domain is tiny -- integers 0..4, three
short strings -- because random 64-bit integers would make `a = b` false
for every pair of rows that ever existed. And NULLs are frequent by
construction rather than by accident.

The NULL rate has an optimum, which is not obvious until it is measured:

| NULL rate | three-valued | partition nothing |
|---|---|---|
| 0.00 | 2% | 53% |
| 0.15 | 27% | 24% |
| **0.25** | **32%** | **13%** |
| 0.45 | 27% | 8% |
| 0.60 | 22% | 13% |

Too few NULLs and the third partition is empty. Too many and the rows are
mostly NULL, so the *other* two empty out instead and the predicate becomes
constant. At 0 it is 2% rather than 0% only because a literal `NULL` can
still appear inside the predicate itself.

## What does not work

**It has never found a bug in SQLite, and probably will not.** SQLite is
among the most heavily tested software in existence, and the published runs
of these exact techniques already took what was there. A run reporting
nothing is the expected outcome. What this has to prove is that it *would*
report something, which is what `selftest` is for and why the planted-bug
table is the first thing in this file.

No other engines. There is no DuckDB or PostgreSQL on this machine -- which
is the reason for choosing a metamorphic oracle over differential testing,
rather than a limitation of it.

No floating point, no collation-dependent operators, no `LIKE`, no
triggers, views, foreign keys, virtual tables or window functions. Row
order is never compared, only sorted multisets, because SQL does not
promise an order without `ORDER BY` and comparing sequences would report a
bug every time the planner legitimately changed its mind.

The reducer shrinks rows, the predicate tree and values. It does not
shrink the SCHEMA -- an unused column stays in the `CREATE TABLE` even when
no surviving part of the predicate mentions it, which is visible in the
worked example above.

`GROUP BY` is partitioned only for `COUNT(*)`, and only on a single
grouping column. `HAVING` is never generated, which is a real gap: it is
where the grouped equivalent of a pushed-down predicate goes wrong.

Only two tables are joined, never three, so join *reordering* -- which is
where a planner has the most freedom to be wrong -- is barely exercised.

## The one hand-written thing, and why

`tlp.py` transcribes the SQL standard's three-valued truth tables, and
checks all 34,353 expressions of the grammar up to depth 2 against SQLite.

That is the only place here where an expected answer is written down by
hand, which is the thing this project otherwise refuses to do, so it is
labelled a **calibration** rather than an oracle. It exists to prove the
model of three-valued logic the generator rests on matches the engine
before anything is built on it. The fuzzer's oracle is the metamorphic
relation, which needs no tables at all.

And because a conformance check that passes on its first run has not been
shown to work, the suite breaks it four ways -- an `AND` where FALSE does
not dominate, an `OR` where TRUE does not, a `NOT` that turns NULL into
FALSE, an `=` that does not propagate NULL -- and asserts it notices each.

A fifth rewrite survives and is recorded as surviving: changing `a is b` to
`a == b` in the `IS` table changes nothing, because on the domain
`{True, False, None}` Python's two operators agree everywhere. An
equivalent mutant rather than a hole.

## Tests

```
$ make test
86 passed, 0 failed
```

No pytest, no network, no dependencies. `sqlite3` is in the standard
library, which is the whole toolchain.

## License

GPL-3.0-or-later.
