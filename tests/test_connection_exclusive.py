import asyncio

from civ_mcp.connection import GameConnection


def test_exclusive_window_blocks_other_tasks_until_release():
    async def scenario():
        conn = GameConnection()
        entered = asyncio.Event()

        async def background_command():
            await conn._wait_for_exclusive()
            entered.set()

        task = asyncio.create_task(background_command())
        async with conn.exclusive():
            await asyncio.sleep(0)
            assert not entered.is_set()

        await asyncio.wait_for(entered.wait(), timeout=0.5)
        await task

    asyncio.run(scenario())
