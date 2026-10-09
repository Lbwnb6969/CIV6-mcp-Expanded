"""Single native save-load dispatch with no UI handlers or screen fallback."""
import asyncio
import re

from civ_mcp.lua._helpers import SENTINEL


async def load_native_test_save(conn, save_name: str) -> str:
    if not re.fullmatch(r"GC_[A-Za-z0-9_]+", save_name):
        return "Error: Native acceptance requires a named GC_ test save"
    async with conn.exclusive():
        await conn.ensure_connected()
        if any(name in {"GameCore_Tuner", "InGame"} for name in conn.lua_states.values()):
            return "Error: Native load requires leaving the independent game first"
        menus = [index for index, name in conn.lua_states.items() if name == "MainMenu"]
        if len(menus) != 1:
            return "Error: Native main-menu context missing or ambiguous"
        index = menus[0]
        completed=getattr(conn,"_native_completed_save_load",None)
        completed_lua='"'+completed+'"' if completed else 'nil'
        code = f'''
assert(UI.IsInFrontEnd())
if not ExposedMembers then ExposedMembers={{}} end
local previous=ExposedMembers.GCNativeLoadIntent
if previous then
    if previous.Name=={completed_lua} and previous.State=="DISPATCHED" then previous.State="COMPLETE" end
    -- A query can finish with a definitive local failure before any native
    -- load was dispatched. Archive those terminal outcomes and permit a new
    -- named request; an unknown QUERYING/DISPATCHED intent remains a hard
    -- no-replay barrier because the engine may still be loading it.
    local terminal=previous.State=="COMPLETE" or previous.State=="NOT_FOUND"
        or previous.State=="REQUIREMENTS_FAILED" or previous.State=="REJECTED"
    assert(terminal,"Existing native load intent; no replay")
    local history=ExposedMembers.GCNativeLoadHistory or {{}}
    history[#history+1]=previous
    ExposedMembers.GCNativeLoadHistory=history
end
GameConfiguration.SetToPreGame()
ExposedMembers.GCNativeLoadIntent={{Name="{save_name}",State="QUERYING"}}
local function results(files,qid)
    LuaEvents.FileListQueryResults.Remove(results)
    UI.CloseFileListQuery(qid)
    local selected
    for _,save in ipairs(files or {{}}) do
        if save.Name=="{save_name}.Civ6Save" and save.IsDirectory==false and save.Type==SaveTypes.SINGLE_PLAYER then
            assert(selected==nil,"Duplicate native test save")
            selected=save
        end
    end
    if not selected then
        ExposedMembers.GCNativeLoadIntent.State="NOT_FOUND"
        print("[GC_LOAD] NOT_FOUND")
        return
    end
    local requirements=Modding.CheckRequirements(selected.RequiredMods or {{}},SaveTypes.SINGLE_PLAYER)
    if requirements~=nil and requirements.Success~=true then
        ExposedMembers.GCNativeLoadIntent.State="REQUIREMENTS_FAILED"
        print("[GC_LOAD] REQUIREMENTS_FAILED")
        return
    end
    ExposedMembers.GCNativeLoadIntent.State="DISPATCHED"
    print("[GC_LOAD] DISPATCHED|{save_name}")
    -- This context is already in the front end. LeaveGame can discard the
    -- pending native load transition here; no active game is replaced.
    local result=Network.LoadGame(selected,ServerType.SERVER_TYPE_NONE)
    if result==false then ExposedMembers.GCNativeLoadIntent.State="REJECTED" end
    print("[GC_LOAD] RETURN|"..tostring(result))
end
LuaEvents.FileListQueryResults.Add(results)
UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE,SaveTypes.SINGLE_PLAYER,
    SaveLocationOptions.NORMAL+SaveLocationOptions.LOAD_METADATA)
print("[GC_LOAD] QUERY_SENT")
print("{SENTINEL}")'''
        try:
            await conn.execute_in_state_once(index, code, timeout=5.0)
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            # A transition can close this state after dispatch. Only inspect.
            pass
        from civ_mcp.game_lifecycle import _wait_for_loaded_game
        # YnAMP giant saves can finish after the former 25-second window.
        # Keep observing the one dispatched load; never send it again.
        if await _wait_for_loaded_game(conn, timeout=120.0):
            # Only a native GC_ world reached through this exact dispatch can
            # complete the front-end intent. Persisted unknown intents survive.
            verified=await conn.execute_in_state_once(conn.ingame_index,f'''
local run=GameConfiguration.GetValue("GC_NATIVE_RUN")
assert(type(run)=="string" and run:match("^GC_[A-Za-z0-9_]+$"))
assert(string.sub("{save_name}",1,#run)==run,"Unexpected native loaded world")
assert(GameConfiguration.IsAnyMultiplayer()==false and GameConfiguration.IsHotseat()==false)
local receipt=ExposedMembers and ExposedMembers.GCNativeLoadIntent
if receipt then
    assert(receipt.Name=="{save_name}" and receipt.State=="DISPATCHED")
    receipt.State="COMPLETE"
end
print("[GC_LOAD] VERIFIED|"..run.."|"..Game.GetCurrentGameTurn())
print("{SENTINEL}")''',timeout=5.0)
            if not any(line.startswith("[GC_LOAD] VERIFIED|") for line in verified):
                return "Error: Native loaded world unverified; intent retained"
            conn._native_completed_save_load=save_name
            return f"Loading save: {save_name}. Native GameCore/InGame detected; verify run and turn."
        return "Error: Native load transition unconfirmed; intent retained, no replay or screen fallback"
