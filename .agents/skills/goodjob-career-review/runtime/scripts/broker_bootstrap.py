#!/usr/bin/env python3
"""Statically checked broker bootstrap with a post-construction readiness handshake."""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

RUNTIME_DIR = Path(__file__).resolve().parents[1]
TRUSTED_SOURCE_DIR = RUNTIME_DIR / "src"
sys.path.insert(0, str(TRUSTED_SOURCE_DIR))

from goodjob.platform.launcher_preflight import broker_ready_marker  # noqa: E402


class SessionBrokerFactory(Protocol):
    def __call__(
        self,
        data_dir: str | None,
        agent_runtime: str | None = None,
        preflight_workspace: str | None = None,
    ) -> object: ...


def _load_session(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("goodjob_launcher_session", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("the session broker module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _signal_ready(readiness_token: str) -> None:
    marker = (broker_ready_marker(readiness_token) + "\n").encode("ascii")
    os.write(1, marker)
    os.write(2, marker)


def run(argv: list[str]) -> int:
    if len(argv) < 2:
        raise RuntimeError("the session broker module is unavailable")
    readiness_token, session_path = argv[:2]
    module = _load_session(Path(session_path))
    base_broker = cast(SessionBrokerFactory, module.SessionBroker)
    session_run = cast(Callable[[list[str]], int], module.run)

    def ready_broker(
        data_dir: str | None,
        agent_runtime: str | None = None,
        preflight_workspace: str | None = None,
    ) -> object:
        broker = base_broker(data_dir, agent_runtime, preflight_workspace)
        _signal_ready(readiness_token)
        return broker

    module.__dict__["SessionBroker"] = ready_broker
    return session_run(argv[2:])


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
