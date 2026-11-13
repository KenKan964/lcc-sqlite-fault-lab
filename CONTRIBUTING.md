# Contributing

Issues and small reproducible examples are welcome. Please use **synthetic** datasets, identify Python/SQLite version, OS, expected and actual JSON evidence, and the exact reproduction command. Do not upload live databases or personal/customer data.

Changes should keep the **OSS-only comparison** independent of `engine.py`, preserve strict sandbox ownership, and add tests for normal behavior and negative control detection. A positive `passed` result for a negative control means the defect detector worked, **not** that the mutant is safe.

Before submitting a pull request:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s . -p test_safety.py -v
```
