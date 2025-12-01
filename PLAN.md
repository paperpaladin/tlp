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
tlp.py reduce  FILE                  shrink a found case to a minimal one
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
- [ ] 06 `tlp reduce` -- shrink a failing case to a minimal schema,
      dataset and predicate
      why: an unreduced report is a thousand rows and a predicate nobody
           will read, which is the same as no report.

### Later

- [ ] 07 partition on a JOIN's ON clause rather than a WHERE, which is
      where the published work found the most bugs
- [ ] 08 aggregate TLP: SUM, COUNT, MIN and MAX over the three partitions
      must reassemble, which needs a different composition rule per
      aggregate and is where this gets interesting
- [ ] 09 DISTINCT and GROUP BY partitioning

## Open questions

- Whether to generate `LIKE` and `GLOB`. They are a rich source of
  behaviour but drag in collation, which the hard invariant excludes.
  Probably yes with `BINARY` pinned explicitly.
- Reduction strategy: delta debugging over the predicate tree is the
  obvious approach, but shrinking the DATA is usually what makes a report
  readable, and the two interact.
