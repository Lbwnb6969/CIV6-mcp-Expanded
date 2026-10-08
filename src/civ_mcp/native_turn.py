"""Bounded native test turns with explicit informational ESC opt-in, no replay."""
import asyncio
import re
import time

from civ_mcp.lua._helpers import SENTINEL
from civ_mcp.native_popups import skip_information_popups as skip_native_information


async def advance_native_turn(gs, expected_run: str, *, timeout: float = 40.0,
                              skip_information_popups: bool = False) -> str:
    if not re.fullmatch(r"GC_[A-Za-z0-9_]+", expected_run):
        raise ValueError("Independent run required")
    conn = gs.conn
    if type(skip_information_popups) is not bool:
        raise ValueError("skip_information_popups must be a boolean")
    gs._native_turn_receipt = None
    gs._native_popup_receipts = []
    async with conn.exclusive():
        await conn.ensure_connected()
        core = [i for i, name in conn.lua_states.items() if name == "GameCore_Tuner"]
        ui = [i for i, name in conn.lua_states.items() if name == "InGame"]
        if len(core) != 1 or len(ui) != 1:
            raise ConnectionError("Independent game context missing or ambiguous")

        async def inspect():
            lines = await conn.execute_in_state_once(core[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
local turn=Game.GetCurrentGameTurn()
local pending=Game:GetProperty("GC_NATIVE_TURN_PENDING")
if pending~=nil then
    assert(type(pending)=="table" and pending.Schema==1 and pending.Run=="{expected_run}")
    assert(type(pending.Turn)=="number" and pending.Turn>=0 and pending.Turn==math.floor(pending.Turn))
end
print("[GC_TURN] "..turn.."|"..tostring(pending and pending.Turn or -1))
print("{SENTINEL}")''', timeout=5.0)
            rows = [line.removeprefix("[GC_TURN] ").split("|") for line in lines if line.startswith("[GC_TURN] ")]
            if len(rows) != 1 or len(rows[0]) != 2:
                raise ConnectionError("Native turn receipt unavailable")
            turn, pending = map(int, rows[0])
            if turn < 0 or pending < -1:
                raise ConnectionError("Native turn receipt invalid")
            return turn, pending

        async def ui_complete(turn):
            lines = await conn.execute_in_state_once(ui[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(GameConfiguration.IsAnyMultiplayer()==false and GameConfiguration.IsHotseat()==false)
local player=Players[Game.GetLocalPlayer()]
assert(player and player:IsHuman())
print("[GC_UI_TURN] "..Game.GetCurrentGameTurn().."|"..tostring(player:IsTurnActive()))
print("{SENTINEL}")''', timeout=5.0)
            rows = [line.removeprefix("[GC_UI_TURN] ").split("|") for line in lines if line.startswith("[GC_UI_TURN] ")]
            if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] not in {"true", "false"}:
                raise ConnectionError("Native UI turn receipt unavailable")
            return int(rows[0][0]) == turn and rows[0][1] == "true"

        async def complete(pending, after):
            if not await ui_complete(after):
                return False
            await conn.execute_in_state_once(core[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(Game.GetCurrentGameTurn()=={after})
Game:SetProperty("GC_NATIVE_TURN_PENDING",nil)
print("{SENTINEL}")''', timeout=5.0)
            gs._native_pending_turn = None
            gs._native_turn_receipt = (pending, after)
            return True

        before, pending = await inspect()
        recovered = getattr(gs, "_native_pending_turn", None)
        if pending < 0 and recovered and recovered[0] == expected_run:
            pending = recovered[1]
        if pending >= 0 and before < pending:
            return "BLOCKED: native pending turn conflicts with the loaded turn"
        if pending >= 0 and before > pending:
            if skip_information_popups:
                gs._native_popup_receipts.extend(await skip_native_information(conn, expected_run))
            if await complete(pending, before):
                return f"Native turn advanced from {pending} to {before}; previous request was not replayed"

        if pending >= 0 and recovered and recovered[0] == expected_run:
            await conn.execute_in_state_once(core[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
if Game:GetProperty("GC_NATIVE_TURN_PENDING")==nil then
    Game:SetProperty("GC_NATIVE_TURN_PENDING",{{Schema=1,Run="{expected_run}",Turn={pending}}})
end
print("{SENTINEL}")''', timeout=5.0)

        if pending < 0:
            if skip_information_popups:
                gs._native_popup_receipts.extend(await skip_native_information(conn, expected_run))
            ready = await conn.execute_in_state_once(ui[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(GameConfiguration.IsAnyMultiplayer()==false and GameConfiguration.IsHotseat()==false)
local id=Game.GetLocalPlayer()
assert(Players[id] and Players[id]:IsHuman())
local frequency=Options.GetUserOption("Gameplay","AutoSaveFrequency")
local keep=Options.GetUserOption("Gameplay","AutoSaveKeepCount")
local meetings=0
for _,other in ipairs(PlayerManager.GetAliveMajors()) do
    local otherID=other:GetID()
    if otherID~=id and DiplomacyManager.FindOpenSessionID(id,otherID)~=nil then meetings=meetings+1 end
end
print("[GC_READY] "..tostring(UI.CanEndTurn()).."|"..tostring(Players[id]:IsTurnActive()).."|"..tostring(frequency).."|"..tostring(keep).."|"..meetings)
print("{SENTINEL}")''', timeout=5.0)
            rows = [line.removeprefix("[GC_READY] ").split("|") for line in ready if line.startswith("[GC_READY] ")]
            valid = len(rows) == 1 and len(rows[0]) == 5 and rows[0][:2] == ["true", "true"] and rows[0][4] == "0"
            if valid:
                try:
                    valid = int(rows[0][2]) > 0 and int(rows[0][3]) > 0
                except ValueError:
                    valid = False
            if not valid:
                return "BLOCKED: native local turn, positive autosave settings, or closed diplomacy unavailable; no popup or screen fallback"
            gs._native_pending_turn = (expected_run, before)
            # Persist before dispatch. A lost receipt cannot cause another send.
            await conn.execute_in_state_once(core[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(Game.GetCurrentGameTurn()=={before} and Game:GetProperty("GC_NATIVE_TURN_PENDING")==nil)
Game:SetProperty("GC_NATIVE_TURN_PENDING",{{Schema=1,Run="{expected_run}",Turn={before}}})
assert(Game:GetProperty("GC_NATIVE_TURN_PENDING").Turn=={before})
print("{SENTINEL}")''', timeout=5.0)
            await conn.execute_in_state_once(ui[0], f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(GameConfiguration.IsAnyMultiplayer()==false and GameConfiguration.IsHotseat()==false)
assert(Game.GetCurrentGameTurn()=={before} and UI.CanEndTurn() and Players[Game.GetLocalPlayer()]:IsTurnActive())
assert(Options.GetUserOption("Gameplay","AutoSaveFrequency")>0 and Options.GetUserOption("Gameplay","AutoSaveKeepCount")>0)
UI.RequestAction(ActionTypes.ACTION_ENDTURN)
print("{SENTINEL}")''', timeout=5.0)
            pending = before

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            if skip_information_popups:
                gs._native_popup_receipts.extend(await skip_native_information(conn, expected_run))
            after, _ = await inspect()
            if after > pending:
                if await complete(pending, after):
                    return f"Native turn advanced from {pending} to {after}"
        return f"BLOCKED: native core/UI turn not complete after {pending}; request retained, no replay"
