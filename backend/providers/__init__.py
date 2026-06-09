"""Market data provider package.

The first provider refactor stage moves shared market-data helpers out of
`backend.main`. Concrete providers can now be split into this package without
dragging route code with them.
"""

