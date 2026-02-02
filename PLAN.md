# tlp -- plan

## Contract

`tlp` looks for queries where SQLite returns the **wrong rows**: a result
that differs from what the data says it should be, with no error raised and
nothing obvious to compare against.

```
tlp.py truth   [--depth N]           three-valued logic, exhaustively
tlp.py vacuity [--n N]               how often the generator is interesting
tlp.py tlp     [--n N] [--seed S]    ternary partitioning
tlp.py norec   [--n N] [--seed S]    optimised against unoptimised
tlp.py agg     [--n N] [--seed S]    aggregate partitioning
tlp.py join    [--n N] [--seed S]    partition a join's ON clause
tlp.py distinct [--n N] [--seed S]   partition a DISTINCT
tlp.py group   [--n N] [--seed S]    partition a GROUP BY
tlp.py having  [--n N] [--seed S]    partition a HAVING clause
tlp.py selftest                      plant bugs, check the oracles notice
tlp.py joinselftest                  the same, for the join relations
tlp.py reduce  [--bug K]             shrink a found case to a minimal one
```

Exit status: 0 nothing found, 1 a disagreement was found, 2 usage error.

## Hard invariant

**Every disagreement reported is a real one.**

A tool that cries wolf about a database that is behaving correctly is worse
than no tool, because the cost of each report is a human reading a query
and a schema. So the bar is not "the two results differ" but "the two
results differ AND the relation between them is one SQL guarantees".

That rules out, by construction rather than by filtering afterwards:

- Anything whose row order is unspecified. Every comparison is between
  sorted multisets, never between result sequences.
- Floating point. Generated data is integers, text and NULL only.
- Any predicate whose value depends on collation, locale or the current
  time.

The second half of the invariant is the one with teeth: **a run that finds
nothing has to be distinguishable from a run that tested nothing.** See the
oracle below.

## Oracle

Ranked by how little of it we control.

1. **The metamorphic relation itself.** This is the whole point and it is
   unusually strong: it needs no second implementation, no stored expected
   output, and no model of what the query means.

   TLP (Rigger and Su, OOPSLA 2020). SQL is three-valued, so for any
   predicate `p` every row makes `p` exactly one of TRUE, FALSE or NULL,
   and the three partitions must reassemble into the whole:

       SELECT * FROM t WHERE p
         UNION ALL SELECT * FROM t WHERE NOT p
         UNION ALL SELECT * FROM t WHERE p IS NULL
       ==  SELECT * FROM t

   NoREC (Rigger and Su, ESEC/FSE 2020). The same query written so the
   optimiser cannot use an index must return the same count as the form
   where it can:

       SELECT COUNT(*) FROM t WHERE p
       ==  SELECT SUM(p IS TRUE) FROM t

   Neither can be satisfied by being self-consistently wrong in the way a
   hand-written expectation can.

2. **The SQL standard's three-valued truth tables**, transcribed in
   `tlp.py` and checked exhaustively against SQLite. This is a
   CALIBRATION, not the oracle, and the distinction matters: the tables are
   written down by hand here, which is exactly the thing this project
   otherwise avoids. They exist to prove the model of three-valued logic
   the generator is built on matches the engine, before anything is built
   on it. 34,353 expressions at depth 2.

3. **The generator's own vacuity measurement.** A predicate that is never
   NULL leaves TLP's third arm empty and reduces the test to "SQLite agrees
   with itself about TRUE and FALSE", which it will, forever, while
   printing a large and encouraging number. The fraction of generated
   predicates that actually produce a NULL is therefore measured and
   reported, not assumed.

## Non-goals

Crash finding. Fuzzers that look for segfaults in SQLite already exist and
are better at it; this is for wrong answers, which they cannot see.

Other engines. There is no DuckDB or PostgreSQL on this machine, and a
metamorphic oracle does not need one -- which is the reason for choosing
this technique over differential testing rather than a limitation of it.

Finding a bug in SQLite. SQLite is among the most heavily tested software
in existence and the published results for these techniques found most of
what was there. A run that reports nothing is the expected outcome; what
this has to prove is that it WOULD report something, which is what the
planted-defect checks are for.

Schema features beyond what the generator emits: no triggers, no views, no
foreign keys, no virtual tables, no window functions.

## Commit sequence

- [x] 00 three-valued logic, exhaustively, and the check broken on purpose
      why: everything else rests on SQL's three values behaving the way the
           generator assumes. Calibrate first, then build.
      files: tlp.py t/test_tlp.py Makefile
      proves: 34,353 expressions at depth 2 agree; four wrong truth tables
              are each caught; one rewrite is recorded as an equivalent
              mutant rather than chased.
- [x] 01 schema and data generation, with NULLs by construction
      why: a table with no NULLs cannot exercise three-valued logic at all,
           so the NULL rate is a parameter rather than an accident.
- [x] 02 the predicate generator, and `tlp vacuity`
      why: the generator decides whether any of this means anything, and
           the measurement has to exist before the fuzzer that depends on
           it. Expect the first version to be mostly two-valued.
- [x] 03 the TLP oracle
- [x] 04 the NoREC oracle
      why: TLP partitions the rows; NoREC attacks the optimiser directly.
           They fail on different bugs and neither subsumes the other.
- [x] 05 planted bugs: a wrapper that corrupts results on purpose, so the
      oracles can be watched to fail
      why: this is the only way to know the tool works, since the thing it
           is pointed at is almost certainly correct.
      done: four bug classes, and the table is the justification for
            carrying two oracles rather than one:
              drop-row           TLP 100%,  NoREC 0%
              duplicate-row      TLP 100%,  NoREC 0%
              null-only          TLP  23%,  NoREC 0%
              count-off-by-one   TLP   0%,  NoREC 100%
            TLP never asks for a count, so it cannot see one that is
            wrong; NoREC only ever compares two numbers, so it cannot see
            which rows moved. Neither subsumes the other, measured rather
            than asserted.
      found: the narrow bug was not narrow. "null-only" was meant to fire
            only for predicates mentioning NULL and was written as
            `"IS NULL" in sql` -- but the partition query ends in
            `(p) IS NULL` whatever p is, so it fired every time and was
            caught 100% of the time, exactly like the broad ones. Counting
            occurrences instead takes it to 23%, which is the honest shape
            for a narrow bug met by a random generator.
      note: the NULL rate has an optimum, which was not obvious until it
            was measured. At 0 nothing is three-valued; at 0.6 the rows
            are so NULL-heavy that the TRUE and FALSE arms empty out
            instead and 13% of predicates become constant. 0.25 is the
            peak at 32%, and the sweep is in the README.
- [x] 06 `tlp reduce` -- shrink a failing case to a minimal schema,
      dataset and predicate
      why: an unreduced report is a thousand rows and a predicate nobody
           will read, which is the same as no report.
      done: greedy delta debugging over three axes -- drop a row, replace a
            predicate node with one of its own subtrees, pull a value
            toward NULL. Needed the generator refactored to hand back a
            TREE rather than a finished string; `gen_predicate` is now
            `render(gen_tree(...))` so every existing oracle is unchanged,
            which was checked by the suite staying green at the same
            numbers across the refactor.
      proves: SOUNDNESS, which is the only property that matters -- what
            comes out must still fail. A reducer that shrinks past the bug
            emits a minimal case reproducing nothing and sends its reader
            hunting for a bug that is not there, which is worse than
            printing the unreduced case. Asserted on every reduction the
            suite performs.
            And because the bugs are planted their true floors are known,
            so effectiveness is a measurement rather than a smoke test:
              drop-row       fires above 3 rows  -> reduces to exactly 4
              duplicate-row  fires on any rows   -> reduces to exactly 1
              null-only      fires above 2 rows  -> reduces to exactly 3
            "Smaller than before" would be satisfied by a reducer that
            drops one row and gives up.
            The null-only case is the one worth looking at: that bug also
            requires the predicate to mention NULL, the reducer was told
            neither condition, and it landed on 3 rows and `c0 IS NULL`.
            It rediscovered the trigger by shrinking.
      found: the shrink order was not well-founded and the reducer did not
            terminate. The value axis offered both NULL and 0 to every
            value and counted any accepted swap as progress, so NULL became
            0 and 0 became NULL forever, each step locally an improvement.
            Candidates are now strictly below the current value on a total
            order and the suite asserts no candidate leads back. Removing
            the strictness makes the suite HANG rather than fail, which is
            how that mutation honestly shows up.
      found: the reduced-case printer emitted Python, not SQL. Rows came
            out as `INSERT INTO t VALUES (None, None);` -- and the entire
            value of a reduced case is that it can be pasted into sqlite3.
            The suite now re-executes the printed INSERTs and compares the
            multiset it gets back.
      found: and that round-trip test was itself broken, exposed by a
            mutation rather than by a passing run. It compared with
            `sorted()`, which raises on a row mixing NULL with an integer;
            every case that happened to be green was all-NULL, so it never
            fired. Comparing `Counter`s is both correct and the right
            relation, since these are multisets.
      survived: breaking soundness -- dropping a row without re-checking --
            is caught for drop-row and null-only but NOT for
            duplicate-row, which is an equivalent mutant: that bug fires on
            any non-empty result, so dropping rows unconditionally lands on
            its real floor of 1 and the case still fails. Recorded rather
            than chased.

### Later

- [x] 07 partition on a JOIN's ON clause rather than a WHERE, which is
      where the published work found the most bugs
      done: two relations, both derived and checked by hand on a worked
            4x3 example before anything was built on them.
            INNER: the three ON-partitions are a partition of the cross
            product, because every pair makes the predicate exactly one of
            the three values and an inner join keeps the TRUE ones.
            LEFT: the same partitioning does NOT hold -- each arm pads its
            own unmatched rows and the padding is counted three times. The
            decomposition does: a left join is its inner join plus one row
            per left row with no TRUE match. That brings NOT EXISTS into
            the picture, which is a correlated subquery the optimiser
            rewrites into something else entirely.
            Every generated ON predicate is forced to mention both tables.
            Without it the predicate is a filter the planner pushes down
            and the join is not the join being tested. 65% genuinely split
            the cross product.
      proves: join-drop-pair  INNER 100%, LEFT   0%
              leftjoin-overpad INNER  0%, LEFT 100%
            Kept as its own table rather than folded into the single-table
            one, because those oracles query t and these query t1 and t2 --
            "TLP does not catch this" would only mean they were never
            pointed at the same thing.
      found: a hand-written expectation was wrong, again. The worked
            example asserts the join keeps 3 pairs; the first version said
            2. SQLite was right. The case stays, with the three TRUE pairs
            enumerated in the comment, because a relation that has only
            ever met generated data is a relation nobody has checked -- if
            it were wrong the generator would produce thousands of cases
            agreeing with it.
- [x] 08 aggregate TLP: SUM, COUNT, MIN and MAX over the three partitions
      must reassemble, which needs a different composition rule per
      aggregate and is where this gets interesting
      done: SUM recombines with SUM, MIN with MIN, MAX with MAX -- and
            COUNT with SUM, because counts add rather than counting. AVG
            is left out rather than approximated: the average of three
            averages is not the average, and weighting them needs the
            counts, which is a different query.
      proves: the rules are load-bearing, which matters because they look
            arbitrary. Against a correct database each right rule raises
            0 alarms in 210 checks and each wrong one raises 125 to 210.
            COUNT is the one to stare at: writing COUNT as its own
            combiner, which is the obvious mistake, reports a bug in 208
            of 210 correct queries.
            And it catches what the other two cannot: an aggregate wrong
            while every row is present and every count agrees is caught
            85% of the time by this oracle and 0% by either other.
      found: the planted bug for it was hitting the wrong query. It fired
            on anything starting "SELECT SUM(", which includes NoREC's own
            unoptimised form, so the table showed NoREC catching it 100%
            of the time -- what NoREC had actually noticed was the test
            wrapper breaking NoREC. Excluding subqueries gives the honest
            row: 0%, 0%, 85%.
- [x] 09 DISTINCT and GROUP BY partitioning
      why: each is the partitioning done over a query shape where the
           obvious recombination is the wrong one, and the two are wrong in
           OPPOSITE directions, which is the most instructive version of
           the trap the aggregate rules set.
      done: DISTINCT recombines with UNION -- a set union, and the only
            relation here that is not UNION ALL -- because a value can
            appear in two partitions: two rows sharing it can land on
            opposite sides of the predicate.
            GROUP BY must recombine with UNION ALL inside, for exactly the
            reason DISTINCT must not: each arm holds part of every group
            and the three partial counts are the point, so a set union
            would collapse two arms that counted the same number of rows.
            The outer step is then COUNT-recombines-with-SUM, group by
            group.
      proves: each rule is a false-alarm generator in the other's place,
            against a correct database, in 200 checks:
              DISTINCT   UNION 0,  UNION ALL 113, INTERSECT 200, EXCEPT 198
              GROUP BY   SUM   0,  COUNT 196,     MIN 113,       MAX/AVG 113
            And each relation catches exactly its own class: a dedup
            skipped on one path is caught 100% by DISTINCT and 0% by the
            four others; one group counted wrong is caught 100% by GROUP BY
            and 0% by the four others. Both leave every row present, every
            count right and every whole-table aggregate right.
      note: the 113 is one number appearing four times, and it is one
            event seen from two sides -- a value landing in more than one
            partition. UNION ALL then keeps it twice; the per-arm counts
            then sum above any single arm. Measured (113 of 200 span a
            partition) rather than left as a coincidence.
      note: the DISTINCT relation rests on DISTINCT, UNION and GROUP BY all
            treating two NULLs as the same value while `=` does not. If
            UNION followed the `=` rule a NULL from two arms would survive
            twice and the relation would be false for every table holding
            one. All three operators are checked directly.
      found: a zero in the selftest table that did not mean what it looked
            like. DISTINCT reads 0% against drop-row, which reads as "it
            cannot see a lost row" -- but drop-row is written to fire on
            "UNION ALL" and this relation recombines with "UNION", so the
            0% is a property of the trigger string. Added setop-drop-row,
            the same bug on any set operation: the column moves to 80%, so
            the relation sees lost rows and it was the bug that could not
            see the relation. That row of the table exists only to answer
            that question, and without it the table overclaims.
      found: and a vacuous test of my own, caught by mutation rather than
            by a run. Both false-alarm checks passed the composition rule
            in explicitly -- false_alarms(distinct_check, combine="UNION")
            -- so they tested a configuration no caller uses and left the
            shipped default unchecked. Changing the default to UNION ALL
            kept both green. A parameterised test that always supplies the
            parameter has stopped testing the thing that ships; they now
            call with no keyword and assert the defaults separately.
      found: run_oracle and cmd_agg printed found cases as Python reprs
            rather than SQL, the same defect the reducer had. Both now use
            sql_literal.

- [x] 10 HAVING partitioning
      why: HAVING is where the grouped equivalent of a pushed-down
           predicate goes wrong, and item 09 deliberately left it out.
           The relation is not the obvious one: a HAVING clause filters
           GROUPS, so partitioning it does not partition rows at all.
      done: and the recombination turned out to be the SIMPLEST here, in
            the exact place item 09 needed the most care. Plain UNION ALL,
            no re-aggregation: the groups are formed by the GROUP BY before
            HAVING runs, so each one arrives whole in exactly one arm,
            already counted. Item 09 needed SUM(n) because partitioning a
            WHERE cuts every group into three pieces; partitioning a
            HAVING cuts no group at all.
      proves: what is load-bearing MOVES. For DISTINCT and GROUP BY the
            composition operator was the trap; here it is not a trap and
            the traps are all in the arms, in 200 checks:
              as shipped                        0
              UNION instead of UNION ALL        0   <- equivalent rewrite
              drop the IS NULL arm             88
              IS NOT TRUE beside an IS NULL arm 88
              a duplicated TRUE arm           136
            UNION costs nothing because each arm emits at most one row per
            group key and the key is unique within a group, so the
            recombined query never holds two identical rows. The one place
            in this project where the obvious-looking mistake is not one,
            and recorded rather than quietly relied on.
      proves: having-drop-group is caught 98% by this relation and 0% by
            the five others. Nothing else writes a HAVING clause.
      found: THE THREE-WAY SPLIT IS A CHOICE, NOT A CORRECTNESS
            REQUIREMENT, which changed what I thought this project was
            doing. `h IS NOT TRUE` is exactly FALSE-or-NULL, so two arms
            already cover every group once, and the two-arm split raises 0
            false alarms in 200. It is not a quirk of HAVING either: the
            same two-arm split holds for the row-level relation this
            project is NAMED after, checked directly over 200 queries.
            "Ternary" describes how the technique is usually written, not
            something it needs to be valid.
      found: and the obvious follow-up claim -- that three arms is at
            least the STRONGER test, since it issues a third query shape --
            is not supported by anything measured here. Three arms and two
            arms detect drop-row, duplicate-row, null-only and
            setop-drop-row at identical rates.
            That is a finding about the PLANTED BUGS, not the relations.
            They corrupt result sets rather than query plans, so they
            cannot tell two sound relations apart however differently a
            real engine would execute them. It is the ceiling of this whole
            selftest approach and now says so in the README, because the
            planted-bug table is the first thing in that file and would
            otherwise read as measuring more than it does.
      found: the vacuity is structurally capped, not merely low. The
            generator reaches 18% three-valued against the row level's 32%,
            and an eight-row table has about 3.9 groups -- splitting four
            things three ways without emptying an arm is harder than
            splitting eight. Measured rather than excused.
            The aggregate mix is the lever: SUM, MIN and MAX go NULL on an
            all-NULL group while the COUNTs never do, so a COUNT-only
            generator leaves the third arm permanently empty (0%). 10%
            COUNT terms at NULL rate 0.40 is the measured optimum, and the
            sweep has the same interior peak as the row-level NULL rate for
            the same reason. `having` is the one command whose NULL-rate
            default is not 0.25.
      found: a self-comparing test of mine, caught by mutation. The check
            that the shipped arms are the correct ones compared
            having_check.__defaults__ against tlp.HAVING_ARMS -- the
            constant under test -- so it passed for any value that constant
            held. Cutting the shipped arms to a two-way split left it
            green. The literal tuple is now written out.
- [ ] 11 a third table in the join, so reordering is actually exercised
      why: item 07 joins exactly two tables, which gives the planner
           almost no freedom to reorder -- and reordering is where it has
           the most freedom to be wrong. The README says so plainly and
           that admission is the reason this is next rather than later.
- [ ] 12 LIKE and GLOB, with BINARY pinned explicitly
      why: a rich source of behaviour, and the reason it has been deferred
           is in the open questions below. Pinning the collation is what
           keeps it inside the hard invariant rather than outside it.

## Open questions

- Whether to generate `LIKE` and `GLOB`. They are a rich source of
  behaviour but drag in collation, which the hard invariant excludes.
  Probably yes with `BINARY` pinned explicitly.
- RESOLVED by item 06. Reduction does both, and the data axis matters
  more: rows go 10 -> 1 while predicates go 13 nodes -> 1. The two
  interact in the direction that helps, since a smaller predicate makes
  more rows droppable. The axis that caused trouble was neither -- it was
  shrinking VALUES, which needs a well-founded order or the loop runs
  forever.
- Whether the schema should shrink too. An unused column survives into the
  reduced CREATE TABLE even when nothing in the surviving predicate
  mentions it. Cheap to add and it would make reports shorter; left out
  because dropping a column changes the DDL rather than the data, and
  every oracle here assumes the schema is fixed for the life of a case.
