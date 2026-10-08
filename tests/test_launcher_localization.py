"""Regression tests for the Windows/Chinese Civ6 save-loader path."""

from civ_mcp.game_launcher import _find_save_name_match, _find_steam_exe_win32


def test_save_prefix_match_handles_dx11_ocr_confusion():
    results = [
        ("GC DXII BAS E �� N E", 1071, 354, 176, 12),
        ("other save", 1071, 394, 120, 12),
    ]
    match = _find_save_name_match(
        results, "GC_DX11_BASELINE_20261002_1655_T1"
    )
    assert match is not None
    assert match[0].startswith("GC DXII")


def test_save_prefix_match_rejects_short_ambiguous_names():
    results = [("GC DXII BAS E", 1071, 354, 120, 12)]
    assert _find_save_name_match(results, "GC") is None


def test_steam_executable_is_selected_from_steam_path(monkeypatch, tmp_path):
    steam_dir = tmp_path / "Steam"
    steam_dir.mkdir()
    steam_exe = steam_dir / "steam.exe"
    steam_exe.write_text("")
    monkeypatch.setenv("SteamPath", str(steam_dir))
    assert _find_steam_exe_win32() == str(steam_exe)
