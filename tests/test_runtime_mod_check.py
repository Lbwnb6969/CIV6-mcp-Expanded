from scripts.codex_runtime_mod_check import DEFAULT_EXPECTED_MODS


def test_runtime_mod_check_tracks_all_local_test_mods():
    assert DEFAULT_EXPECTED_MODS == {
        "Endgame": "2af0c438-bab0-4d0c-987f-0beaf0f7c7f2",
        "Stability": "7b9b91d9-6ce7-4fc7-93c2-1c170ccbf401",
        "TestFlow": "b7f8e82d-2ef8-4db3-a1a7-91ac8c5d7f13",
    }
