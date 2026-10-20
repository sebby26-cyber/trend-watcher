"""Explicit consumer registry (no discovery): name -> module."""
from . import investment

REGISTRY = {investment.NAME: investment}


def namespace(module, cfg):
    return "{}@{}".format(module.NAME, cfg[module.NAME]["env"])
