# Geography test fixtures (SYNTHETIC, test-only)

Everything in this folder is synthetic test data for `tests/test_geo_*.py`. The running app never loads
it. The county "polygons" are plain rectangles roughly where the real counties are, and every place
coordinate was made up to sit inside those rectangles. Place names are real Long Island names (so the
alias and disambiguation rules can be tested), but none of these coordinates are real and none may be
copied into `data/` or the built gazetteer.
