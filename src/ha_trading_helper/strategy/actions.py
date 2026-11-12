"""What a signal tells the user to do (its own module so explanations can use it)."""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    ENTER = "ENTRAR"
    WAIT = "ESPERAR"
    EXIT = "SALIR"
