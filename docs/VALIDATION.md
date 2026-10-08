# Validation scope

Export baseline: local source commit `9e3c31f5459ed65503ee12820eaf799807e25eb1`, derived from lmwilki/civ6-mcp. File hashes are in SOURCE_MANIFEST.json. Private game logs and test saves are retained outside this public repository.

Windows Steam native acceptance has demonstrated exclusive resident ownership; strict native save loading; actual core/UI turn completion; original informational ESC callbacks for tech/civic, disaster, project, and wonder contexts; mod diagnostics; and state reads across save/reload. These results establish the tested operations, not every gameplay command or mod feature. Natural-wonder movie closure has not independently completed its live acceptance case.

Game rules and UI vary by context. Missing or ambiguous contexts, unknown player identity, incomplete transport receipts, and conflicting pending turns remain blocked. Visual layout, physical buttons/focus, arbitrary mod compatibility, large-map performance, and non-Windows native acceptance are outside the completed MCP evidence.

Release checks: 206/206 offline tests in a fresh Windows Python 3.14 environment; wheel and sdist built; actual wheel installed; imports/version and 77-tool registration verified outside the checkout; native-service help and pip check passed. Tested dependencies: mcp 1.30.0, fastapi 0.143.0, anthropic 0.125.0. An initial MCP 2.3.0 collection failure was fixed by the declared `mcp<2` bound. The source/archive audit passed its configured secret/path/member checks.

These checks do not replace live game evidence. The new installed `civ6-native-service` entry point wraps the same service design, but its packaging checks are distinct from the existing resident service's live history. Python 3.12 Windows/Linux GitHub Actions results must be evaluated separately after publication.
