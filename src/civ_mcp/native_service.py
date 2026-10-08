"""Installed entry point for the isolated native acceptance HTTP service."""
from contextlib import asynccontextmanager
import argparse
import logging
import os
import socket

import uvicorn
from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState
from civ_mcp.web_api import create_app
from civ_mcp.native_session import reserve_native_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tuner-port", type=int, default=4318, choices=(4318, 4319))
    parser.add_argument("--pending-run", help="Preserve a previous unknown end-turn request across service restart")
    parser.add_argument("--pending-turn", type=int)
    args = parser.parse_args()
    if (args.pending_run is None) != (args.pending_turn is None):
        parser.error("pending-run and pending-turn must be supplied together")
    if args.pending_turn is not None and args.pending_turn < 0:
        parser.error("pending-turn must be nonnegative")
    logging.basicConfig(level=logging.INFO)
    conn = GameConnection(port=args.tuner_port, replay_on_disconnect=False, native_acceptance_owner=True)
    gs = GameState(conn)
    if args.pending_run is not None:
        gs._native_pending_turn = (args.pending_run, args.pending_turn)
    app = create_app(gs)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await conn.disconnect()

    app.router.lifespan_context = lifespan
    with reserve_native_session(), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 8000))
        config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
        uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
