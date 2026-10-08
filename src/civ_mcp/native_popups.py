"""Explicitly authorized informational ESC callbacks on the resident connection.

The caller owns conn.exclusive(). This is neither an input emulator nor a
general popup recovery path. Every invocation requires an independent SP run.
"""
import re

from civ_mcp.lua._helpers import SENTINEL


HANDLERS = {
    "TechCivicCompletedPopup": "assert(type(TryClose)=='function'); TryClose()",
    "NaturalDisasterPopup": "assert(type(KeyHandler)=='function' and Keys.VK_ESCAPE~=nil); assert(KeyHandler(Keys.VK_ESCAPE)==true)",
    "ProjectBuiltPopup": "assert(type(KeyHandler)=='function' and Keys.VK_ESCAPE~=nil); assert(KeyHandler(Keys.VK_ESCAPE)==true)",
    "WonderBuiltPopup": "assert(type(KeyHandler)=='function' and Keys.VK_ESCAPE~=nil); assert(KeyHandler(Keys.VK_ESCAPE)==true)",
    "NaturalWonderPopup": "assert(type(KeyHandler)=='function' and Keys.VK_ESCAPE~=nil); assert(KeyHandler(Keys.VK_ESCAPE)==true)",
}


async def skip_information_popups(conn, expected_run: str) -> list[dict]:
    if not re.fullmatch(r"GC_[A-Za-z0-9_]+", expected_run):
        raise ValueError("Independent run required")
    targets = []
    for name, handler in HANDLERS.items():
        indexes = [i for i, n in conn.lua_states.items() if n == name]
        if len(indexes) > 1:
            raise ConnectionError(f"Ambiguous informational popup: {name}")
        if indexes:
            targets.append((indexes[0], name, handler))
    results = []
    for index, name, handler in targets:
        # One normal ESC per call. A queued successor is a separate displayed
        # notification, handled on a subsequent bounded turn poll. Transport
        # loss propagates: the command is never automatically replayed.
        lines = await conn.execute_in_state_once(index, f'''
assert(GameConfiguration.GetValue("GC_NATIVE_RUN")=="{expected_run}")
assert(GameConfiguration.IsAnyMultiplayer()==false and GameConfiguration.IsHotseat()==false)
assert(ContextPtr:GetID()=="{name}")
local hidden=ContextPtr:IsHidden()
assert(type(hidden)=="boolean")
if hidden then print("[GC_INFO_POPUP] HIDDEN") else
    {handler}
    print("[GC_INFO_POPUP] ESC|"..tostring(ContextPtr:IsHidden()))
end
print("{SENTINEL}")''', timeout=5.0)
        rows = [line.removeprefix("[GC_INFO_POPUP] ") for line in lines if line.startswith("[GC_INFO_POPUP] ")]
        if len(rows) != 1 or rows[0] not in {"HIDDEN", "ESC|true", "ESC|false"}:
            raise ConnectionError(f"Informational popup receipt unavailable: {name}")
        if rows[0] != "HIDDEN":
            results.append({"context": name, "callback": "ESC", "hidden_after": rows[0] == "ESC|true"})
    return results
