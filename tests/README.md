# Test organization

The test tree mirrors `src/miner`. Put a module's behavior tests in the
corresponding package and name the test file after the production module.
Cross-module behavior belongs at the nearest shared package level.

Keep tests for current public behavior and the core mining, validation,
runtime, scanner, and tool contracts. Remove tests that only prove a deleted
option, legacy implementation, or one-off historical failure stays absent.
Prefer one representative boundary case over exhaustive combinations unless
the combinations express distinct supported behavior.
