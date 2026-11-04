#!/usr/bin/env python3
"""
Exercise implementations for Fitness Robot.
"""

from .bicep_curls import BicepCurlsRunner, run_bicep_curls
from .pushups import PushupsRunner, run_pushups
from .squats import SquatsRunner, run_squats
from .plank import PlankRunner, run_plank

__all__ = [
    "BicepCurlsRunner", "run_bicep_curls",
    "PushupsRunner", "run_pushups",
    "SquatsRunner", "run_squats",
    "PlankRunner", "run_plank"
]
