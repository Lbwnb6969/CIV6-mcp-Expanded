"""Game lifecycle — popup dismissal, save/load, raw Lua execution."""

from __future__ import annotations

import logging
import shutil

from civ_mcp import lua as lq
from civ_mcp.connection import GameConnection

log = logging.getLogger(__name__)


async def dismiss_popup(conn: GameConnection) -> str:
    """Dismiss any blocking popup or UI overlay in the game.

    Three-phase approach:
    1. Single batched InGame call that checks all known popup/overlay names
       and closes diplomacy screens (fast — one TCP roundtrip).
    2. Only if Phase 1 found nothing: scan individual Lua states for
       ExclusivePopupManager popups (disaster, wonder, era screens) that
       need Close() in their own state to release the engine event lock.
    3. Safety net: always fire ExclusivePopupManager Close LuaEvents to
       ensure BulkHide counters are decremented even if Phase 1 caught
       the popup by name (SetHide) without proper cleanup.
    """
    dismissed = []

    # Phase 1: Single batched InGame call — handles most cases in one roundtrip.
    # Covers: diplomacy screens, generic popups, world congress, boosts, etc.
    # NOTE: ExclusivePopupManager popups (NaturalDisaster, NaturalWonder,
    # WonderBuilt, EraComplete, RockBand, ProjectBuilt) are handled ONLY in
    # Phase 2 via Close() in their own Lua state.  Phase 1's SetHide() breaks
    # Phase 2's IsHidden check without releasing the PopupManager lock.
    popup_names = [
        "InGamePopup",
        "GenericPopup",
        "PopupDialog",
        "BoostUnlockedPopup",
        "GreatWorkShowcase",
        "WorldCongressPopup",
        "WorldCongressIntro",
    ]
    checks = []
    for name in popup_names:
        checks.append(
            f'do local c = ContextPtr:LookUpControl("/InGame/{name}") '
            f"if c and not c:IsHidden() then "
            f"  pcall(function() UIManager:DequeuePopup(c) end) "
            f"  pcall(function() Input.PopContext() end) "
            f"  c:SetHide(true) "
            f'  print("DISMISSED|{name}") '
            f"end end"
        )
    # LeaderScene 3D model: SetHide does NOT clear the C++ 3D viewport.
    # Must fire Events.HideLeaderScreen() to unload the 3D leader model.
    checks.append(
        'do local ls = ContextPtr:LookUpControl("/InGame/LeaderScene") '
        "if ls and not ls:IsHidden() then "
        "  pcall(function() Events.HideLeaderScreen() end) "
        "  ls:SetHide(true) "
        '  print("DISMISSED|LeaderScene") '
        "end end"
    )
    # Diplomacy screens: report only, do NOT close sessions.
    # Force-closing sessions via DiplomacyManager.CloseSession() bypasses
    # the C++ engine's session lifecycle callbacks, leaving the AI diplomacy
    # subsystem in an inconsistent state that causes turn processing hangs
    # (confirmed across Games 1-5).  Use respond_to_diplomacy() instead.
    checks.append(
        'do local dv = ContextPtr:LookUpControl("/InGame/DiplomacyActionView") '
        "if dv and not dv:IsHidden() then "
        '  print("PENDING|DiplomacyActionView") '
        "end end"
    )
    # NOTE: DiplomacyDealView is NOT dismissed here — it represents an
    # incoming trade deal offer that the agent must accept/reject via
    # get_pending_trades + respond_to_trade.  Dismissing it silently kills
    # the offer (e.g. incoming delegations from other civs).
    checks.append(
        'do local ddv = ContextPtr:LookUpControl("/InGame/DiplomacyDealView") '
        "if ddv and not ddv:IsHidden() then "
        '  print("PENDING|DiplomacyDealView") '
        "end end"
    )
    # Camera reset for cinematic mode
    checks.append(
        "local mode = UI.GetInterfaceMode() "
        "if mode == InterfaceModeTypes.CINEMATIC then "
        '  pcall(function() UI.ClearTemporaryPlotVisibility("NaturalDisaster") end) '
        '  pcall(function() UI.ClearTemporaryPlotVisibility("NaturalWonder") end) '
        "  pcall(function() Events.StopAllCameraAnimations() end) "
        "  pcall(function() UILens.RestoreActiveLens() end) "
        "  UI.SetInterfaceMode(InterfaceModeTypes.SELECTION) "
        '  print("DISMISSED|cinematic_camera") '
        "end"
    )
    pending_deal = False
    pending_diplomacy = False
    try:
        lua = " ".join(checks) + f' print("{lq.SENTINEL}")'
        lines = await conn.execute_write(lua)
        for line in lines:
            if line.startswith("DISMISSED|"):
                dismissed.append(line.split("|", 1)[1])
            elif line.startswith("PENDING|"):
                if "DiplomacyDealView" in line:
                    pending_deal = True
                elif "DiplomacyActionView" in line:
                    pending_diplomacy = True
    except Exception as e:
        log.debug("Phase 1 dismiss failed: %s", e)

    # Pre-check: single InGame call to detect visible ExclusivePopupManager
    # popups.  Phase 2 scans ~30 Lua states individually (~450ms each = ~13.5s)
    # to find these.  This pre-check costs one round-trip (~500ms) and skips
    # Phase 2+3 entirely when no ExclusivePopups are active (>99% of calls).
    exclusive_popup_names = [
        "TechCivicCompletedPopup",
        "NaturalWonderPopup",
        "NaturalDisasterPopup",
        "WonderBuiltPopup",
        "EraCompletePopup",
        "HistoricMoments",
        "MomentPopup",
        "ProjectBuiltPopup",
        "RockBandPopup",
        "RockBandMoviePopup",
    ]
    any_exclusive_visible = False
    try:
        precheck_lua = (
            " ".join(
                f'do local c = ContextPtr:LookUpControl("/InGame/{n}") '
                f'if c and not c:IsHidden() then print("EXCL_VISIBLE") end end'
                for n in exclusive_popup_names
            )
            + f' print("{lq.SENTINEL}")'
        )
        precheck_lines = await conn.execute_write(precheck_lua)
        any_exclusive_visible = any("EXCL_VISIBLE" in l for l in precheck_lines)
    except Exception as e:
        log.debug("ExclusivePopup pre-check failed (will run Phase 2): %s", e)
        any_exclusive_visible = True  # fail-open: scan if pre-check errors

    if any_exclusive_visible:
        log.info("ExclusivePopup visible — running Phase 2 state scan")

        # Phase 2: Close ExclusivePopupManager popups in their own Lua states.
        # These need Close() in their OWN state to release the engine lock —
        # Phase 1's SetHide() does NOT release this lock.
        popup_keywords = ("Popup", "Wonder", "Moment", "Era", "Disaster")
        popup_states = {
            idx: n
            for idx, n in conn.lua_states.items()
            if any(kw in n for kw in popup_keywords)
        }
        log.debug("Phase 2 popup states: %s", popup_states)
        for state_idx, name in popup_states.items():
            # Loop to drain the ExclusivePopupManager's engine queue —
            # each Close() pops the next event, so we keep closing until
            # the popup stays hidden (max 20 to avoid infinite loops).
            for _drain in range(20):
                try:
                    lines = await conn.execute_in_state(
                        state_idx,
                        "pcall(function() if m_kQueuedPopups then m_kQueuedPopups = {} end end); "
                        "if not ContextPtr:IsHidden() then "
                        "  local ok = pcall(Close); "
                        "  if not ok then pcall(OnClose) end; "
                        '  print("DISMISSED") '
                        "end; "
                        'print("---END---")',
                    )
                    if any("DISMISSED" in l for l in lines):
                        dismissed.append(name)
                    else:
                        break  # popup stayed hidden, queue drained
                except Exception as e:
                    log.debug(
                        "Popup check failed for %s (state %d): %s",
                        name,
                        state_idx,
                        e,
                    )
                    break

        # Phase 3: Fallback — if InGame still sees visible ExclusivePopups,
        # probe state indexes to find and close them.  Handles cases where
        # lua_states from the handshake is incomplete (truncated LSQ).
        try:
            check_lua = (
                " ".join(
                    f'do local c = ContextPtr:LookUpControl("/InGame/{n}") '
                    f'if c and not c:IsHidden() then print("STILL_VISIBLE|{n}") end end'
                    for n in exclusive_popup_names
                )
                + f' print("{lq.SENTINEL}")'
            )
            still_visible = await conn.execute_write(check_lua)
            remaining = [
                l.split("|", 1)[1]
                for l in still_visible
                if l.startswith("STILL_VISIBLE|")
            ]
            if remaining:
                log.info(
                    "Phase 3: ExclusivePopups still visible after Phase 2: %s "
                    "(probing state indexes...)",
                    remaining,
                )
                for probe_idx in range(50, 200):
                    if probe_idx in popup_states:
                        continue
                    if not remaining:
                        break
                    try:
                        probe_lines = await conn.execute_in_state(
                            probe_idx,
                            'print(ContextPtr:GetID()); print("---END---")',
                            timeout=1.0,
                        )
                        state_name = probe_lines[0] if probe_lines else ""
                        if state_name not in remaining:
                            continue
                        close_lines = await conn.execute_in_state(
                            probe_idx,
                            "pcall(function() if m_kQueuedPopups then m_kQueuedPopups = {} end end); "
                            "local ok = pcall(Close); "
                            "if not ok then pcall(OnClose) end; "
                            "ContextPtr:SetHide(true); "
                            'print("DISMISSED"); '
                            'print("---END---")',
                            timeout=2.0,
                        )
                        if any("DISMISSED" in l for l in close_lines):
                            dismissed.append(f"{state_name} (probed state {probe_idx})")
                            remaining.remove(state_name)
                            conn.lua_states[probe_idx] = state_name
                            log.info(
                                "Phase 3: Dismissed %s at state %d",
                                state_name,
                                probe_idx,
                            )
                    except Exception:
                        pass
        except Exception as e:
            log.debug("Phase 3 probe failed: %s", e)

    # Final phase: dismiss Windows-level crash dialogs (Firaxis Crash
    # Reporter, Unhandled Exception).  These are Win32 dialogs that appear
    # on top of the game after EXCEPTION_ACCESS_VIOLATION crashes — the
    # game keeps running but Lua calls return degraded data until dismissed.
    from . import game_launcher

    crash_dismissed = await game_launcher.dismiss_crash_dialogs()
    dismissed.extend(crash_dismissed)

    if dismissed:
        msg = f"Dismissed: {', '.join(dismissed)}"
        if pending_diplomacy:
            msg += ". Also: diplomacy session active — use respond_to_diplomacy."
        if pending_deal:
            msg += " (incoming trade deal pending — use get_pending_trades)"
        return msg
    if pending_diplomacy:
        return "Diplomacy session active — use respond_to_diplomacy to handle it."
    if pending_deal:
        return "No popups to dismiss (incoming trade deal pending — use get_pending_trades)."
    return "No popups to dismiss."


# ------------------------------------------------------------------
# Save / Load
# ------------------------------------------------------------------


async def save_game(conn: GameConnection, name: str) -> str:
    """Create a named save. Used for MCP per-turn autosaves."""
    lines = await conn.execute_write(
        f"local gf = {{}}; "
        f'gf.Name = "{name}"; '
        f"gf.Location = SaveLocations.LOCAL_STORAGE; "
        f"gf.Type = SaveTypes.SINGLE_PLAYER; "
        f"gf.IsAutosave = false; "
        f"gf.IsQuicksave = false; "
        f"Network.SaveGame(gf); "
        f'print("OK|{name}"); '
        f'print("{lq.SENTINEL}")'
    )
    if any("OK|" in l for l in lines):
        return f"Saved: {name}"
    return f"Save may have failed: {' '.join(lines)}"


def cleanup_old_autosaves(keep: int = 5) -> None:
    """Move MCP autosaves older than the most recent ``keep`` to an archive.

    Named saves are evidence and may be needed to reproduce a run. Keep the
    active directory bounded while preserving every evicted file under the
    dedicated ``_abandoned_mcp`` folder.
    """
    import glob
    import os

    from .game_launcher import SINGLE_SAVE_DIR

    pattern = os.path.join(SINGLE_SAVE_DIR, "0_MCP_*.Civ6Save")
    saves = glob.glob(pattern)
    if len(saves) <= keep:
        return
    saves.sort(key=os.path.getmtime, reverse=True)
    archive_dir = os.path.join(SINGLE_SAVE_DIR, "_abandoned_mcp")
    os.makedirs(archive_dir, exist_ok=True)
    for old in saves[keep:]:
        try:
            basename = os.path.basename(old)
            destination = os.path.join(archive_dir, basename)
            if os.path.exists(destination):
                stem, suffix = os.path.splitext(basename)
                stamp = int(os.path.getmtime(old))
                attempt = 0
                while os.path.exists(destination):
                    marker = f"_{stamp}" if attempt == 0 else f"_{stamp}_{attempt}"
                    destination = os.path.join(archive_dir, f"{stem}{marker}{suffix}")
                    attempt += 1
            shutil.move(old, destination)
            log.debug("Archived old MCP autosave: %s -> %s", old, destination)
        except OSError as e:
            log.debug("Failed to archive %s: %s", old, e)


async def list_saves(conn: GameConnection) -> str:
    """List available saves (normal + autosave).

    Uses filesystem scan (reliable — finds all save types including
    autosaves and quicksaves). Falls back to Lua query if filesystem
    scan finds nothing.
    """
    result = _list_saves_filesystem()
    if "No saves found" not in result:
        return result

    # Fallback: Lua-based query (may miss autosaves/quicksaves)
    lua_result = await _list_saves_lua(conn)
    if lua_result is not None:
        return lua_result
    return result


async def _list_saves_lua(conn: GameConnection) -> str | None:
    """Try Lua-based save enumeration. Returns None on failure."""
    try:
        await conn.execute_write(
            f"if not ExposedMembers then ExposedMembers = {{}} end; "
            f"ExposedMembers.MCPSaveList = nil; "
            f"ExposedMembers.MCPSaveQueryDone = false; "
            f"local function OnResults(fileList, qid) "
            f"  ExposedMembers.MCPSaveList = fileList; "
            f"  ExposedMembers.MCPSaveQueryDone = true; "
            f"  UI.CloseFileListQuery(qid); "
            f"  LuaEvents.FileListQueryResults.Remove(OnResults); "
            f"end; "
            f"LuaEvents.FileListQueryResults.Add(OnResults); "
            f"local opts = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE + SaveLocationOptions.QUICKSAVE + SaveLocationOptions.LOAD_METADATA; "
            f"UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, opts); "
            f'print("QUERY_SENT"); '
            f'print("{lq.SENTINEL}")'
        )

        import asyncio

        for _ in range(20):
            await asyncio.sleep(0.25)
            check_lines = await conn.execute_write(
                f"if ExposedMembers.MCPSaveQueryDone then "
                f"  local fl = ExposedMembers.MCPSaveList; "
                f"  if fl and #fl > 0 then "
                f'    print("COUNT|" .. #fl); '
                f"    for i, s in ipairs(fl) do "
                f'      if i <= 20 then print("SAVE|" .. i .. "|" .. tostring(s.Name)) end '
                f"    end "
                f'  else print("EMPTY") end '
                f'else print("PENDING") end; '
                f'print("{lq.SENTINEL}")'
            )
            if any(l.startswith("COUNT|") or l == "EMPTY" for l in check_lines):
                results = [l for l in check_lines if l.startswith("SAVE|")]
                if not results:
                    return None  # empty — fall through to filesystem
                lines_out = ["Available saves (use load_save with the index number):"]
                for r in results:
                    parts = r.split("|", 2)
                    idx = parts[1]
                    name = parts[2] if len(parts) > 2 else "?"
                    lines_out.append(f"  {idx}. {name}")
                return "\n".join(lines_out)
    except Exception:
        pass
    return None  # timed out or error — fall through to filesystem


def _list_saves_filesystem() -> str:
    """Scan the save directory on disk (always works)."""
    import glob
    import os

    from .game_launcher import SAVE_DIR

    save_base = os.path.dirname(SAVE_DIR)  # .../Saves/Single
    all_saves: list[tuple[float, str]] = []

    # Autosaves
    for f in glob.glob(os.path.join(SAVE_DIR, "*.Civ6Save")):
        all_saves.append((os.path.getmtime(f), os.path.basename(f)))

    # Normal saves (parent directory)
    for f in glob.glob(os.path.join(save_base, "*.Civ6Save")):
        all_saves.append((os.path.getmtime(f), os.path.basename(f)))

    all_saves.sort(reverse=True)  # newest first
    if not all_saves:
        return "No saves found on filesystem."

    lines = ["Available saves (filesystem scan, sorted by date):"]
    for i, (_mtime, name) in enumerate(all_saves[:25], 1):
        lines.append(f"  {i}. {name.replace('.Civ6Save', '')}")
    return "\n".join(lines)


async def load_save(conn: GameConnection, save_index: int) -> str:
    """Load a save by index from the most recent list_saves() query.

    The game will reload — the FireTuner connection stays alive but
    all Lua state is wiped. Wait a few seconds after calling this.
    """
    lines = await conn.execute_write(
        f"if not ExposedMembers or not ExposedMembers.MCPSaveList then "
        f'  print("ERR:NO_SAVE_LIST"); print("{lq.SENTINEL}"); return '
        f"end; "
        f"local fl = ExposedMembers.MCPSaveList; "
        f"local idx = {save_index}; "
        f"if idx < 1 or idx > #fl then "
        f'  print("ERR:INDEX_OUT_OF_RANGE|" .. #fl); print("{lq.SENTINEL}"); return '
        f"end; "
        f"local save = fl[idx]; "
        f'print("LOADING|" .. tostring(save.Name)); '
        f'print("{lq.SENTINEL}"); '
        f"Network.LeaveGame(); "
        f"Network.LoadGame(save, ServerType.SERVER_TYPE_NONE)"
    )
    for line in lines:
        if line.startswith("ERR:NO_SAVE_LIST"):
            return "Error: No save list cached. Call list_saves() first."
        if line.startswith("ERR:INDEX_OUT_OF_RANGE"):
            count = line.split("|")[1] if "|" in line else "?"
            return f"Error: Index {save_index} out of range (1-{count}). Call list_saves() to see available saves."
        if line.startswith("LOADING|"):
            name = line.split("|", 1)[1]
            return f"Loading save: {name}. Game will reload — wait ~10 seconds then call get_game_overview to verify."
    return "Load command sent. Wait for game to reload."


def _frontend_state_indices(conn: GameConnection) -> list[int]:
    """Return candidate menu states, preferring the active load-menu contexts."""
    priority = {
        "FrontEnd": 0,
        "MainMenu": 1,
        "LoadGameMenu": 2,
        "FrontEnd_Tuner": 3,
    }
    return [
        index
        for index, _name in sorted(
            (
                (index, name)
                for index, name in conn.lua_states.items()
                if name in priority
            ),
            key=lambda item: (priority[item[1]], item[0]),
        )
    ]


def _frontend_state_index(conn: GameConnection) -> int | None:
    """Return the first usable front-end Lua state."""
    states = _frontend_state_indices(conn)
    return states[0] if states else None


def _lua_string(value: str) -> str:
    """Quote a validated save name for a Lua string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


async def _wait_for_loaded_game(conn: GameConnection, timeout: float = 20.0) -> bool:
    """Confirm that a load command produced GameCore and InGame states.

    FireTuner's ``Network.LoadGame`` return value only acknowledges the API
    call.  A real load replaces the front-end state set with GameCore/InGame;
    callers must verify that transition before reporting success.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            await conn.reconnect()
            if conn.gamecore_index is not None and conn.ingame_index is not None:
                return True
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        await asyncio.sleep(1)
    return False


async def _load_save_via_frontend_lua(
    conn: GameConnection, save_name: str
) -> str | None:
    """Load a save from the front-end state without OCR."""
    import asyncio

    state_indices = _frontend_state_indices(conn)
    if not state_indices:
        deadline = asyncio.get_running_loop().time() + 300
        while asyncio.get_running_loop().time() < deadline:
            try:
                await conn.reconnect()
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                await asyncio.sleep(2)
                continue
            state_indices = _frontend_state_indices(conn)
            if state_indices:
                break
            await asyncio.sleep(2)
    if not state_indices:
        log.warning("Native menu load skipped: front-end Lua states never appeared")
        return None
    quoted_name = _lua_string(save_name)

    # Reuse the vanilla entry point so LoadGameMenu is an active popup.  A
    # hidden LoadGameMenu context can enumerate files, but Network.LoadGame
    # from that context may return to the front end without starting a load.
    main_menu_index = next(
        (index for index, name in conn.lua_states.items() if name == "MainMenu"),
        None,
    )
    if conn.gamecore_index is None and main_menu_index is not None:
        try:
            opened = await conn.execute_in_state(
                main_menu_index,
                "if OnLoadSinglePlayer then OnLoadSinglePlayer(); print('LOAD_MENU_OPENED') "
                "else print('LOAD_MENU_UNAVAILABLE') end; "
                f'print("{lq.SENTINEL}")',
                timeout=2.0,
            )
            log.info("Native menu load entry point state %s: %s", main_menu_index, opened)
            await asyncio.sleep(1.5)
            state_indices = _frontend_state_indices(conn)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            log.info("Native menu load entry point could not be invoked")

    # Let the vanilla LoadGameMenu select the entry and invoke its action
    # handler.  This preserves compatibility checks and the loading-screen
    # transition that a direct Network.LoadGame call from a hidden context can
    # bypass.
    load_menu_state = next(
        (index for index in state_indices if conn.lua_states.get(index) == "LoadGameMenu"),
        None,
    )
    if load_menu_state is not None:
        try:
            refreshed = await conn.execute_in_state(
                load_menu_state,
                "if UIManager and ContextPtr then UIManager:QueuePopup(ContextPtr, PopupPriority.Current) end; "
                "if OnShow then OnShow() end; "
                "if OnUpdateUI then OnUpdateUI(0, '', 0, 0, '') end; "
                "print('LOAD_MENU_REFRESHED'); "
                f'print("{lq.SENTINEL}")',
                timeout=2.0,
            )
            log.info("Native LoadGameMenu refresh state %s: %s", load_menu_state, refreshed)
            await asyncio.sleep(1.5)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            log.info("Native LoadGameMenu refresh could not be invoked")

        native_action = (
            "local found = nil; "
            "for i, s in ipairs(g_FileList or {}) do "
            f"  if string.find(tostring(s.Name or ''), {quoted_name}, 1, true) then found = i; break end "
            "end; "
            "if found then "
            "  local save = g_FileList[found]; "
            "  local errors = Modding.CheckRequirements(save.RequiredMods or {}, SaveTypes.SINGLE_PLAYER); "
            "  if errors and not errors.Success then "
            "    print('NATIVE_REQUIREMENTS'); "
            "  else "
            "    local vanilla = false; "
            "    if OnLoadYes and debug and debug.getupvalue and debug.setupvalue then "
            "      local slot = nil; "
            "      for up = 1, 32 do "
            "        local name = debug.getupvalue(OnLoadYes, up); "
            "        if name == 'm_thisLoadFile' then slot = up; break end; "
            "      end; "
            "      if slot then "
            "        local ok = pcall(function() "
            "          debug.setupvalue(OnLoadYes, slot, save); OnLoadYes(); "
            "        end); "
            "        if ok then vanilla = true; print('NATIVE_VANILLA_ONLOADYES') end; "
            "      end; "
            "    end; "
            "    if not vanilla then "
            "      print('NATIVE_ACTION'); "
            "      pcall(function() Network.LeaveGame() end); "
            "      local ok = Network.LoadGame(save, ServerType.SERVER_TYPE_NONE); "
            "      print('NATIVE_LOAD|' .. tostring(ok)); "
            "    end; "
            "  end; "
            "else print('NATIVE_NO_FILE') end; "
            f'print("{lq.SENTINEL}")'
        )
        for _ in range(12):
            try:
                action_lines = await conn.execute_in_state(
                    load_menu_state, native_action, timeout=2.0
                )
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                log.info("Native menu action closed the connection for save %s", save_name)
                if await _wait_for_loaded_game(conn):
                    return f"Loading save: {save_name}. GameCore/InGame detected; call get_game_overview to verify."
                log.warning("Native menu action disconnected without a GameCore/InGame transition")
                break
            if action_lines:
                log.info("Native LoadGameMenu action state %s: %s", load_menu_state, action_lines)
            if (
                "NATIVE_LOAD|true" in action_lines
                or "NATIVE_ACTION" in action_lines
                or "NATIVE_VANILLA_ONLOADYES" in action_lines
            ):
                log.info(
                    "Native LoadGameMenu action accepted save %s in state %s",
                    save_name,
                    load_menu_state,
                )
                if await _wait_for_loaded_game(conn):
                    return f"Loading save: {save_name}. GameCore/InGame detected; call get_game_overview to verify."
                log.warning("Native menu action returned without a GameCore/InGame transition")
                break
            await asyncio.sleep(0.5)

    log.info("Native menu load: querying save %s in Lua states %s", save_name, state_indices)
    # Civ6 dispatches FileListQueryResults asynchronously.  Issue the query in
    # every active front-end context first, then poll them; this is the order
    # proven to work with the native menu's multiple LoadGameMenu instances.
    probe_query = (
        "if not ExposedMembers then ExposedMembers = {} end; "
        "ExposedMembers.MCPLoadProbeDone = false; "
        "ExposedMembers.MCPLoadProbeNames = {}; "
        "local function ProbeResults(fileList, qid) "
        "  pcall(function() UI.CloseFileListQuery(qid) end); "
        "  pcall(function() LuaEvents.FileListQueryResults.Remove(ProbeResults) end); "
        "  for _, s in ipairs(fileList or {}) do "
        "    table.insert(ExposedMembers.MCPLoadProbeNames, tostring(s.Name or '')); "
        "  end; "
        "  ExposedMembers.MCPLoadProbeDone = true; "
        "end; "
        "pcall(function() UI.CloseAllFileListQueries() end); "
        "LuaEvents.FileListQueryResults.Add(ProbeResults); "
        "local opts = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE "
        "  + SaveLocationOptions.QUICKSAVE + SaveLocationOptions.LOAD_METADATA; "
        "UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, opts); "
        'print("QUERY_SENT"); '
        f'print("{lq.SENTINEL}")'
    )
    acknowledged: list[int] = []
    for state_index in state_indices:
        try:
            started = await conn.execute_in_state(state_index, probe_query, timeout=2.0)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            continue
        if "QUERY_SENT" in started:
            acknowledged.append(state_index)
        else:
            log.info("Native menu probe state %s did not acknowledge query: %s", state_index, started)
    if not acknowledged:
        return None

    poll = (
        "print('DONE|' .. tostring(ExposedMembers.MCPLoadProbeDone)); "
        "if ExposedMembers.MCPLoadProbeNames then "
        "  for _, n in ipairs(ExposedMembers.MCPLoadProbeNames) do print('SAVE|' .. n) end "
        "end; "
        f'print("{lq.SENTINEL}")'
    )
    matched_states: list[int] = []
    for _ in range(12):
        await asyncio.sleep(0.25)
        for poll_state in acknowledged:
            try:
                check = await conn.execute_in_state(poll_state, poll, timeout=1.0)
            except (ConnectionError, OSError, asyncio.IncompleteReadError):
                continue
            if any(line.startswith("SAVE|") and save_name in line[5:] for line in check):
                matched_states.append(poll_state)
        if matched_states:
            break
    if not matched_states:
        log.info("Native menu probe did not find save %s", save_name)
        return None
    # Query callbacks are available in several front-end contexts, but the
    # actual load operation belongs to a LoadGameMenu context when one exists.
    load_menu_states = [
        index for index in state_indices if conn.lua_states.get(index) == "LoadGameMenu"
    ]
    matched_state = load_menu_states[0] if load_menu_states else matched_states[0]
    log.info(
        "Native menu probe matched save %s in states %s; loading from state %s (%s)",
        save_name,
        matched_states,
        matched_state,
        conn.lua_states.get(matched_state),
    )

    # The front-end owns the load transition already.  LeaveGame is required
    # for the in-game query path, but calling it from a front-end menu can
    # discard the pending UI transition and return to the main menu.
    leave_before_load = (
        "      pcall(function() Network.LeaveGame() end); "
        if conn.gamecore_index is not None
        else ""
    )
    load_query = (
        "if not ExposedMembers then ExposedMembers = {} end; "
        "ExposedMembers.MCPLoadResult = nil; "
        "ExposedMembers.MCPLoadDone = false; "
        "local function OnResults(fileList, qid) "
        "  pcall(function() UI.CloseFileListQuery(qid) end); "
        "  pcall(function() LuaEvents.FileListQueryResults.Remove(OnResults) end); "
        "  for _, s in ipairs(fileList or {}) do "
        f"    local candidate = tostring(s.Name or ''); "
        f"    if string.find(candidate, {quoted_name}, 1, true) then "
        '      ExposedMembers.MCPLoadResult = "FOUND"; '
        "      ExposedMembers.MCPLoadDone = true; "
        "      local vanilla = false; "
        "      if OnLoadYes and debug and debug.getupvalue and debug.setupvalue then "
        "        local slot = nil; "
        "        for up = 1, 32 do "
        "          local name = debug.getupvalue(OnLoadYes, up); "
        "          if name == 'm_thisLoadFile' then slot = up; break end "
        "        end; "
        "        if slot then "
        "          local ok = pcall(function() "
        "            debug.setupvalue(OnLoadYes, slot, s); OnLoadYes(); "
        "          end); "
        "          if ok then vanilla = true; print('NATIVE_VANILLA_CALLBACK') end; "
        "        end; "
        "      end; "
        "      if not vanilla then "
        f"{leave_before_load}"
        "        Network.LoadGame(s, ServerType.SERVER_TYPE_NONE); "
        "      end; "
        "      return "
        "    end "
        "  end; "
        '  ExposedMembers.MCPLoadResult = "NOT_FOUND"; '
        "  ExposedMembers.MCPLoadDone = true; "
        "end; "
        "pcall(function() UI.CloseAllFileListQueries() end); "
        "LuaEvents.FileListQueryResults.Add(OnResults); "
        "local opts = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE "
        "  + SaveLocationOptions.QUICKSAVE + SaveLocationOptions.LOAD_METADATA; "
        "UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, opts); "
        'print("QUERY_SENT"); '
        f'print("{lq.SENTINEL}")'
    )
    try:
        started = await conn.execute_in_state(matched_state, load_query, timeout=2.0)
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        if await _wait_for_loaded_game(conn):
            return f"Loading save: {save_name}. GameCore/InGame detected; call get_game_overview to verify."
        log.warning("Native query load disconnected without a GameCore/InGame transition")
        return None
    if "QUERY_SENT" not in started:
        return None
    for _ in range(12):
        await asyncio.sleep(0.25)
        try:
            check = await conn.execute_in_state(
                matched_state,
                "if ExposedMembers.MCPLoadDone then "
                'print("RESULT|" .. tostring(ExposedMembers.MCPLoadResult)) '
                "else print('PENDING') end; "
                f'print("{lq.SENTINEL}")',
                timeout=1.0,
            )
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            log.info("Native menu load connection closed after matching save %s", save_name)
            if await _wait_for_loaded_game(conn):
                return f"Loading save: {save_name}. GameCore/InGame detected; call get_game_overview to verify."
            log.warning("Native menu load disconnected without a GameCore/InGame transition")
            return None
        if any(line == "RESULT|FOUND" for line in check):
            log.info("Native menu load accepted save %s in state %s", save_name, matched_state)
            if await _wait_for_loaded_game(conn):
                return f"Loading save: {save_name}. GameCore/InGame detected; call get_game_overview to verify."
            log.warning("Native menu query accepted save %s without a GameCore/InGame transition", save_name)
            return None
        if any(line == "RESULT|NOT_FOUND" for line in check):
            return None
    return None


async def load_game_save(conn: GameConnection, save_name: str) -> str:
    """Load a save by name — no list_saves() prerequisite.

    Two-tier approach:
    1. Lua: query save list, find by name, load in one async operation.
    2. Filesystem: verify file exists, use OCR menu navigation (slow but
       reliable — works for autosaves and quicksaves that Lua can't find).
    """
    import asyncio
    import sys

    if getattr(conn, "native_acceptance_owner", False):
        from civ_mcp.native_load import load_native_test_save
        return await load_native_test_save(conn, save_name)

    # On the Aspyr Linux port, Network.LoadGame silently does nothing
    # (same as Network.SaveGame). Skip Lua tier and go straight to OCR
    # menu navigation which actually works.
    if sys.platform != "linux":
        quoted_name = _lua_string(save_name)
        if conn.gamecore_index is None:
            try:
                # Keep the multi-state query/load sequence together so the
                # resident camera and popup tasks cannot interleave commands
                # while Civ6 is transitioning out of the front end.
                async with conn.exclusive():
                    frontend_result = await _load_save_via_frontend_lua(conn, save_name)
                if frontend_result is not None:
                    return frontend_result
            except Exception:
                log.debug("Front-end Lua load failed for '%s'", save_name, exc_info=True)
        # Tier 1: Lua query-match-load (Windows/macOS only)
        try:
            await conn.execute_write(
                f"if not ExposedMembers then ExposedMembers = {{}} end; "
                f"ExposedMembers.MCPLoadResult = nil; "
                f"ExposedMembers.MCPLoadDone = false; "
                f"local function OnResults(fileList, qid) "
                f"  UI.CloseFileListQuery(qid); "
                f"  LuaEvents.FileListQueryResults.Remove(OnResults); "
                f"  for i, s in ipairs(fileList) do "
                f'    local candidate = tostring(s.Name or ""); '
                f'    if string.find(candidate, {quoted_name}, 1, true) then '
                f'      ExposedMembers.MCPLoadResult = "FOUND"; '
                f"      ExposedMembers.MCPLoadDone = true; "
                f"      Network.LeaveGame(); "
                f"      Network.LoadGame(s, ServerType.SERVER_TYPE_NONE); "
                f"      return "
                f"    end "
                f"  end; "
                f'  ExposedMembers.MCPLoadResult = "NOT_FOUND"; '
                f"  ExposedMembers.MCPLoadDone = true; "
                f"end; "
                f"LuaEvents.FileListQueryResults.Add(OnResults); "
                f"local opts = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE "
                f"  + SaveLocationOptions.QUICKSAVE + SaveLocationOptions.LOAD_METADATA; "
                f"UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, opts); "
                f'print("QUERY_SENT"); '
                f'print("{lq.SENTINEL}")'
            )

            for _ in range(20):
                await asyncio.sleep(0.25)
                check = await conn.execute_write(
                    f"if ExposedMembers.MCPLoadDone then "
                    f'  print("RESULT|" .. tostring(ExposedMembers.MCPLoadResult)) '
                    f'else print("PENDING") end; '
                    f'print("{lq.SENTINEL}")'
                )
                for line in check:
                    if line == "RESULT|FOUND":
                        if await _wait_for_loaded_game(conn):
                            return (
                                f"Loading save: {save_name}. GameCore/InGame detected; "
                                f"call get_game_overview to verify."
                            )
                        log.warning(
                            "Lua query accepted save %s without a GameCore/InGame transition",
                            save_name,
                        )
                        break
                    if line == "RESULT|NOT_FOUND":
                        break  # fall through to Tier 2
                else:
                    continue
                break  # NOT_FOUND — try filesystem

            log.info("Lua query did not find '%s', trying filesystem", save_name)
        except Exception:
            log.debug("Lua load_game_save failed", exc_info=True)
    else:
        log.info(
            "Linux: skipping Lua load (Aspyr port bug), using OCR nav for '%s'",
            save_name,
        )

    # Tier 2: Filesystem verify + OCR menu load
    import os

    from .game_launcher import SAVE_DIR, SINGLE_SAVE_DIR

    auto_path = os.path.join(SAVE_DIR, f"{save_name}.Civ6Save")
    single_path = os.path.join(SINGLE_SAVE_DIR, f"{save_name}.Civ6Save")

    if not os.path.exists(auto_path) and not os.path.exists(single_path):
        return (
            f"Error: Save '{save_name}' not found in Lua query or on filesystem. "
            f"Check the name and try list_saves() to see available saves."
        )

    # File exists but Lua couldn't find it — use OCR menu navigation.
    # If we're at the main menu (no GameCore), navigate directly without
    # restarting. Only restart_and_load if we're in-game.
    from . import game_launcher

    if conn.gamecore_index is None:
        log.info("At main menu — loading '%s' via OCR menu nav", save_name)
        result = await game_launcher.load_save_from_menu(save_name)
    else:
        log.info("In-game — restart_and_load for '%s'", save_name)
        result = await game_launcher.restart_and_load(save_name)

    if await _wait_for_loaded_game(conn, timeout=30.0):
        return f"{result} GameCore/InGame detected."
    return f"{result} Load verification failed: GameCore/InGame not detected."


async def execute_lua(
    conn: GameConnection, code: str, context: str = "gamecore"
) -> str:
    """Escape hatch: run arbitrary Lua code."""
    if context == "ingame":
        lines = await conn.execute_write(code)
    elif context.isdigit():
        lines = await conn.execute_in_state(int(context), code)
    else:
        lines = await conn.execute_read(code)
    return "\n".join(lines) if lines else "(no output)"
