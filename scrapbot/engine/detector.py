"""Detección de fluctuaciones anómalas (Tabla 5, fila 7 / riesgo R3)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    action: str   # 'unchanged' | 'apply' | 'freeze'
    pct: float


def variation_pct(published: int, new: int) -> float:
    if published <= 0:
        return 100.0
    return abs(new - published) / published * 100


def evaluate_change(published: int, new: int, threshold_pct: float) -> Decision:
    """Compara el precio capturado con el publicado.

    Se congela cuando la variación es *superior* al umbral (15 % por defecto).
    """
    if published == new:
        return Decision("unchanged", 0.0)
    pct = round(variation_pct(published, new), 2)
    return Decision("freeze" if pct > threshold_pct else "apply", pct)
