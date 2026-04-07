"""SunSpec model wrapper.

Thin facade over a list of pysunspec2 model instances. Provides a flat
``key`` namespace for points (including repeating-group points like
``module:0:DCA``), filters out points with no value or no unit, and
exposes the calculated (scaled / decoded) value via :meth:`getValue`.

Extracted from ``api.py`` in Phase 4. The class has zero dependencies
on the rest of the integration - it only sees pysunspec2 model objects -
so the move is purely structural.
"""

from __future__ import annotations

from typing import Any


class SunSpecModelWrapper:
    """Wraps the list of pysunspec2 model instances for a single model id.

    A SunSpec device may expose the same model id multiple times (think
    Multiple-MPPT inverters with one ``module`` group per tracker). The
    wrapper carries every instance in ``self._models`` and exposes
    ``num_models`` for callers that need to iterate.
    """

    def __init__(self, models) -> None:
        """Sunspec model wrapper"""
        self._models = models
        self.num_models = len(models)

    def isValidPoint(self, point_name: str) -> bool:
        point = self.getPoint(point_name)
        if point.value is None:
            return False
        if point.pdef["type"] in ("enum16", "bitfield32"):
            return True
        return point.pdef.get("units", None) is not None

    def getKeys(self) -> list[str]:
        keys = list(filter(self.isValidPoint, self._models[0].points.keys()))
        for group_name in self._models[0].groups:
            model_group = self._models[0].groups[group_name]
            if type(model_group) is list:
                for idx, group in enumerate(model_group):
                    key_prefix = f"{group_name}:{idx}"
                    group_keys = map(lambda gp: f"{key_prefix}:{gp}", group.points.keys())
                    keys.extend(filter(self.isValidPoint, group_keys))
            else:
                key_prefix = f"{group_name}:0"
                group_keys = map(lambda gp: f"{key_prefix}:{gp}", model_group.points.keys())
                keys.extend(filter(self.isValidPoint, group_keys))
        return keys

    def getValue(self, point_name: str, model_index: int = 0) -> Any:
        point = self.getPoint(point_name, model_index)
        return point.cvalue

    def getMeta(self, point_name: str) -> dict[str, Any]:
        return self.getPoint(point_name).pdef

    def getGroupMeta(self) -> dict[str, Any]:
        return self._models[0].gdef

    def getPoint(self, point_name: str, model_index: int = 0):
        point_path = point_name.split(":")
        if len(point_path) == 1:
            return self._models[model_index].points[point_name]

        group = self._models[model_index].groups[point_path[0]]
        if type(group) is list:
            return group[int(point_path[1])].points[point_path[2]]
        else:
            if len(point_path) > 2:
                return group.points[point_path[2]]  # Access to the specific point within the group
            return group.points[
                point_name
            ]  # Generic access if no specific subgrouping is specified
